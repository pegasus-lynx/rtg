#!/usr/bin/env python
#
# Author: Thamme Gowda [tg (at) isi (dot) edu]
# Created: 3/12/21

import copy
import gc
import time
from functools import partial
from pathlib import Path
from typing import Optional, Callable, Union, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
import tqdm
from torch.cuda.amp import autocast

from rtg import log, device
from rtg.distrib import DistribTorch
from rtg.eval.clsmetric import ClsMetric
from rtg.exp import TranslationExperiment
from rtg.module import Model
from rtg.module.tfmnmt import (Encoder, EncoderLayer, MultiHeadedAttention, PositionwiseFeedForward,
                               PositionalEncoding, Embeddings)
from rtg.module.trainer import SteppedTrainer, TrainerState, EarlyStopper
from rtg.registry import register, MODEL, ProblemType
from rtg.utils import get_my_args, IO

dtorch = DistribTorch.instance()


class SentenceCompressor(nn.Module):
    """
    Compresses token representation into a single vector
    """

    def __init__(self, d_model: int, attn: MultiHeadedAttention):
        super(SentenceCompressor, self).__init__()
        self.cls_repr = nn.Parameter(torch.zeros(d_model))
        self.d_model = d_model
        self.attn = attn

    def forward(self, src, src_mask):
        B, T, D = src.size()  # [Batch, Time, Dim]
        assert D == self.d_model
        query = self.cls_repr.view(1, 1, D).repeat(B, 1, 1)
        # Args: Query, Key, Value, Mask
        cls_repr = self.attn(query, src, src, src_mask)
        cls_repr = cls_repr.view(B, D)  # [B, D]
        return cls_repr


class Classifier(nn.Module):
    scores = {
        'logits': lambda x, dim=None: x,
        'softmax': F.softmax,
        'log_softmax': F.log_softmax,
        'sigmoid': lambda x, dim=None: x.sigmoid(),
    }

    def __init__(self, d_model: int, n_classes: int):
        super().__init__()
        self.d_model = d_model
        self.n_classes = n_classes
        self.proj = nn.Linear(d_model, n_classes)

    def forward(self, repr, score='logits'):
        score = score or 'logits'
        B, D = repr.shape  # [Batch, Dim]
        assert D == self.d_model
        assert score in self.scores, f'"score", Given={score}, known={list(self.scores.keys())}'
        cls_repr = self.proj(repr)
        return self.scores[score](cls_repr, dim=-1)


class ClassificationExperiment(TranslationExperiment):
    """
        Treat source as source sequence, target as class
        translation is many:many, classification is many:1, a special case of many:many
    """

    def __init__(self, *args, **kwargs):
        super(ClassificationExperiment, self).__init__(*args, **kwargs)
        self.train_db = self.data_dir / 'train.nldb'

    @property
    def problem_type(self) -> ProblemType:
        return ProblemType.CLASSIFICATION

    def pre_process(self, args=None, force=False):
        args = args or self.config.get('prep')
        is_shared = args.get('shared')
        assert not is_shared, 'Shared vocab not supported for Classification.' \
                              ' Please set prep.shared=False'
        # skip TranslationExperiment, go to its parent BaseExperiment pre_process
        super(TranslationExperiment, self).pre_process(args=args, force=force)

        if self.has_prepared() and not force:
            log.warning("Already prepared")
            return

        # check if files are parallel
        self.check_line_count('validation', args['valid_src'], args['valid_tgt'])
        if 'spark' in self.config:
            log.warning(f"Spark backend detected: line count on training data is skipped")
        else:
            log.warning(f"Going to count lines. If this is a big dataset, it will take long time")
            self.check_line_count('training', args['train_src'], args['train_tgt'])

        xt_args = dict(no_split_toks=args.get('no_split_toks'),
                       char_coverage=args.get('char_coverage', 0),
                       min_co_ev=args.get('src_min_co_ev', args.get('min_co_ev', None)))

        src_corpus = [args[key] for key in ['train_src', 'mono_src'] if args.get(key)]
        max_src_size = args.get('max_src_types', args.get('max_types', None))
        assert max_src_size, 'prep.max_src_types or prep.max_types must be defined'
        self.src_field = self._make_vocab("src", self._src_field_file, args['pieces'],
                                          vocab_size=max_src_size, corpus=src_corpus, **xt_args)

        # target vocabulary; class names. treat each line as a word
        tgt_corpus = [args[key] for key in ['train_tgt'] if args.get(key)]

        self.tgt_field = self._make_vocab("tgt", self._tgt_field_file, 'class',
                                          corpus=tgt_corpus, vocab_size=-1)
        n_classes = self.config['model_args'].get('tgt_vocab')
        if len(self.tgt_field) != n_classes:
            log.warning(f'model_args.tgt_vocab={n_classes},'
                        f' but found {len(self.tgt_field)} cls in {tgt_corpus}')

        train_file = self.train_db

        self._pre_process_parallel('train_src', 'train_tgt', out_file=train_file, args=args,
                                   line_check=False)
        self._pre_process_parallel('valid_src', 'valid_tgt', out_file=self.valid_file, args=args,
                                   line_check=False)

        if args.get("finetune_src") or args.get("finetune_tgt"):
            self._pre_process_parallel('finetune_src', 'finetune_tgt', self.finetune_file)
        self.persist_state()
        self._prepared_flag.touch()

        """
        # get samples from validation set
        n_samples = args.get('num_samples', 10)
        space_tokr = lambda line: line.strip().split()
        val_raw_recs = TSVData.read_raw_parallel_recs(
            args['valid_src'], args['valid_tgt'], args['truncate'], args['src_len'],
            args['tgt_len'], src_tokenizer=space_tokr, tgt_tokenizer=space_tokr)
        val_raw_recs = list(val_raw_recs)
        random.shuffle(val_raw_recs)
        samples = val_raw_recs[:n_samples]
        TSVData.write_parallel_recs(samples, self.samples_file)
        """

    def get_predictions(self, model, input, batch_size: Union[int, Tuple[int, int]], max_len=256):
        texts = IO.get_lines(input)
        txt_to_ids = partial(self.src_field.encode_as_ids, add_bos=False, add_eos=False)
        texts = (txt_to_ids(x)[:max_len] for x in texts)
        # sort as descending order of lengths
        texts_lensorted = list(sorted(enumerate(texts), key=lambda x:len(x[1]), reverse=True))
        log.info(f"Predicting labels for {len(texts_lensorted)} sentences")
        model = model.eval().to(device)
        preds = []
        top1_probs = []
        tok_count = 0

        def _consume_minibatch(buffer):
            nonlocal preds, top1_probs  # accessing outer variable
            max_len = max(len(x) for orig_i, x in buffer)
            x_seqs = torch.full((len(buffer), max_len), fill_value=self.src_field.pad_idx,
                                dtype=torch.long)
            batch_is = [batch_i for batch_i, x in buffer]
            for minibatch_i, (batch_i, x) in enumerate(buffer):
                x_seqs[minibatch_i, :len(x)] = torch.tensor(x, dtype=torch.long)

            x_seqs = x_seqs.to(device)
            x_mask = (x_seqs != self.src_field.pad_idx).unsqueeze(1)
            probs = model(src=x_seqs, src_mask=x_mask, score='softmax')
            top_1probs, top_1 = probs.max(dim=1)

            preds += list(zip(batch_is, top_1.tolist()))
            top1_probs += list(zip(batch_is, top_1probs.tolist()))

        if isinstance(batch_size, int):
            max_toks, max_sents = batch_size, float('inf')
        else:
            max_toks, max_sents = batch_size

        buffer = []
        with tqdm.tqdm(texts_lensorted, total=len(texts_lensorted)) as data_bar:
            for idx, txt in data_bar:
                buffer.append((idx, txt))
                tok_count += len(txt)
                if tok_count >= max_toks or len(buffer) >= max_sents:
                    _consume_minibatch(buffer)
                    # new batch
                    buffer.clear()
                    tok_count = 0
            if buffer:
                _consume_minibatch(buffer)

        # restore order, drop indices
        preds = [p for i, p in sorted(preds, key=lambda x:x[0])]
        top1_probs = [p for i, p in sorted(top1_probs, key=lambda x: x[0])]
        return preds, top1_probs

    def evaluate_classifier(self, model, input: Path, labels: Path, batch_size, max_len: int):
        model = model.eval()
        preds, probs = self.get_predictions(model, input, batch_size=batch_size, max_len=max_len)
        label_to_id = partial(self.tgt_field.encode_as_ids, add_bos=False, add_eos=False)
        labels = [label_to_id(x)[0] for x in IO.get_lines(labels)]
        assert len(preds) == len(labels), f'preds:{len(preds)} == truth:{len(labels)}?'
        log.info(f"Testing on {len(labels)} examples")
        clsmap = self.tgt_field.class_names
        metric = ClsMetric(prediction=preds, truth=labels, clsmap=clsmap)
        pred_names = [clsmap[x] for x in preds]
        return metric, pred_names, probs


@register(kind=MODEL)
class TransformerClassifier(Model):
    model_type = 'tfmcls'
    experiment_type = ClassificationExperiment

    EncoderFactory = Encoder
    EncoderLayerFactory = EncoderLayer
    CompressorFactory = SentenceCompressor
    ClassifierFactory = Classifier

    def __init__(self, encoder: Encoder, src_embed, compressor: SentenceCompressor,
                 classifier: Classifier):
        super().__init__()
        self.encoder: Encoder = encoder
        self.src_embed = src_embed
        self.compressor = compressor
        self.classifier = classifier

    @property
    def model_dim(self):
        return self.classifier.d_model

    def encode(self, src, src_mask):
        tok_repr = self.encoder(self.src_embed(src), src_mask)
        return self.compressor(tok_repr, src_mask)

    def forward(self, src, src_mask, score='logits'):
        "Take in and process masked src and target sequences."
        sent_repr = self.encode(src, src_mask)
        if score == 'embedding':  # sentence embedding
            return sent_repr
        return self.classifier(sent_repr, score=score)

    @classmethod
    def make_model(cls, src_vocab: int, tgt_vocab: int, enc_layers=6, hid_size=512, ff_size=2048,
                   n_heads=8, attn_bias=True, attn_dropout=0.1, dropout=0.1, activation='relu',
                   exp: ClassificationExperiment = None):
        "Helper: Construct a model from hyper parameters."

        # get all args for reconstruction at a later phase
        args = get_my_args(exclusions=['cls', 'exp'])
        assert activation in {'relu', 'elu', 'gelu'}
        assert enc_layers > 0, "Zero encoder layers! Hmm🤔"

        log.info(f"Make model, Args={args}")
        c = copy.deepcopy
        attn = MultiHeadedAttention(n_heads, hid_size, dropout=attn_dropout, bias=attn_bias)
        ff = PositionwiseFeedForward(hid_size, ff_size, dropout, activation=activation)
        encoder = cls.EncoderFactory(cls.EncoderLayerFactory(hid_size, c(attn), c(ff), dropout),
                                     enc_layers)
        src_emb = nn.Sequential(Embeddings(hid_size, src_vocab),
                                PositionalEncoding(hid_size, dropout))
        classifier = cls.ClassifierFactory(d_model=hid_size, n_classes=tgt_vocab)
        compressor = cls.CompressorFactory(d_model=hid_size, attn=c(attn))

        model = cls(encoder, src_emb, compressor=compressor, classifier=classifier)

        model.init_params()
        return model, args

    @classmethod
    def make_trainer(cls, *args, **kwargs):
        return ClassifierTrainer(*args, **kwargs)


class ClassifierTrainer(SteppedTrainer):

    def __init__(self, exp: ClassificationExperiment,
                 model: Optional[TransformerClassifier] = None,
                 optim: str = 'ADAM',
                 model_factory=TransformerClassifier.make_model,
                 **optim_args):
        super().__init__(exp, model, model_factory=model_factory, optim=optim, **optim_args)
        assert isinstance(self.core_model, TransformerClassifier), \
            f'Expected an instance of TransformerClassifier; but found {type(self.core_model)}'
        trainer_args = self.exp.config.get('trainer', {}).get('init_args', {})
        chunk_size = trainer_args.get('chunk_size', -1)
        if chunk_size > 0:
            log.warning("chunk_size not supported for this setup; it is ignored")
        self.grad_accum_interval = trainer_args.get('grad_accum', 1)
        assert self.grad_accum_interval > 0

        if self.n_gpus > 1:  # Multi GPU mode
            raise Exception(f"Please use: python -m rtg.distrib.launch -G {self.n_gpus} \n "
                            f" or set single GPU by: export CUDA_VISIBLE_DEVICES=0 ")

        self.classifier = self.core_model.classifier

    def loss_func(self, scores, labels, train_mode=False, take_step=False):
        loss = self.criterion(scores, labels, mask_pad=False).sum() / len(labels)
        if train_mode:  # don't do this for validation set
            dtorch.backward(loss)
            if take_step:
                dtorch.step(self.opt)
        result = loss.item()
        return result

    def run_valid_epoch(self, val_data):
        """
        :param data_iter: data iterator
        :return: loss value
        """
        start = time.time()
        total_loss = 0.0
        num_batches = 0
        model = self.core_model
        assert not model.training
        labels, preds = [], []
        with tqdm.tqdm(val_data, unit='batch', dynamic_ncols=True) as data_bar:
            for i, batch in enumerate(data_bar):
                with autocast(enabled=dtorch.fp16):
                    if self.n_gpus <= 1:  # if not dataparallel, then move
                        batch = batch.to(device)
                    x_mask = (batch.x_seqs != batch.pad_val).unsqueeze(1)
                    scores = self.model(src=batch.x_seqs, src_mask=x_mask,
                                        score=self.criterion.input_type)
                    loss = self.loss_func(scores=scores, labels=batch.ys,
                                          train_mode=False, take_step=False)

                    total_loss += loss
                    num_batches += 1
                    elapsed = time.time() - start
                    data_bar.set_postfix_str(
                        f'Loss:{loss:.4f}, {int(len(batch) / elapsed)}item/s', refresh=False)

                    labels += batch.ys.tolist()
                    if self.criterion.input_type == 'logits':
                        probs = F.softmax(scores, dim=1)
                        _, top_1 = probs.max(dim=1)
                        preds += top_1.tolist()
                start = time.time()

        class_names = self.exp.tgt_vocab.class_names
        metrics = ClsMetric(prediction=preds, truth=labels, clsmap=class_names)

        self.tbd.add_scalars('val_performance',
                             dict(macrof1=metrics.macro_f1, accuracy=metrics.accuracy,
                                  microf1=metrics.micro_f1), self.opt.curr_step)
        if len(class_names) < 40:
            self.tbd.add_scalars('val_f1', dict(zip(metrics.clsmap, metrics.f1)),
                                 self.opt.curr_step)
            self.tbd.add_scalars('val_precision', dict(zip(metrics.clsmap, metrics.precision)),
                                 self.opt.curr_step)
            self.tbd.add_scalars('val_recall', dict(zip(metrics.clsmap, metrics.recall)),
                                 self.opt.curr_step)
        log_conf_mat = len(class_names) < 40
        log.info(f"validation at step={self.opt.curr_step}\n{metrics.format(confusion=log_conf_mat)}")
            
        loss_avg = total_loss / num_batches
        return loss_avg, metrics

    def train(self, steps: int, check_point: int, batch_size: int, log_interval=10,
              check_pt_callback: Optional[Callable] = None, keep_models=10, sort_by='random',
              keep_in_mem=False, early_stop=None, fine_tune=False, **args):

        """
        :param steps: how many optimizer steps to train (also, means how many batches)
        :param check_point: after how many checkpoints to
        :param batch_size: how many target tokens in batch max ( = max_len * num_sentences)
        :param check_pt_callback: function to call back after checkpt
        :param keep_models: how many checkpts to keep
        :param keep_in_mem: keep training data in memory
        :param early_stop: {patience: N validations, by: loss, enabled: True}
        :param args: any extra args
        :return:
        """

        # Gradient accumulation
        opt_steps = steps
        batches = steps * self.grad_accum_interval
        start_batch = self.start_step * self.grad_accum_interval
        check_point = check_point * self.grad_accum_interval
        if isinstance(batch_size, int):
            max_toks, max_sents = batch_size, float('inf')
        else:
            max_toks, max_sents = batch_size
        if args:
            # no extra args. let user know if an extra arg is passed
            raise Exception(f" Found extra args: {args}")
        log.info(f'Going to train for {opt_steps} optimizer steps over {batches} batches'
                 f' (from {self.start_step} steps);'
                 f' batch_size={batch_size} toks; sort_by={sort_by};')

        distr = DistribTorch.instance()
        if batches <= start_batch:
            raise Exception(f'The model was already trained to {self.start_step} steps. '
                            f'Please increase the steps or clear the existing models')

        train_data = self.exp.get_train_data(
            batch_size=batch_size, steps=batches - start_batch, sort_by=sort_by, batch_first=True,
            keep_in_mem=keep_in_mem, fine_tune=fine_tune, y_is_cls=True)
        val_data = None
        if distr.is_global_main:
            val_data = self.exp.get_val_data(batch_size=max_toks, shuffle=False, batch_first=True,
                                             sort_desc=False, y_is_cls=True)

        train_state = TrainerState(self.model, check_point=check_point, unit='item')
        train_state.train_mode(True)
        unsaved_state = False

        batch_count = -1
        stopper = None
        early_stopped = False  # or converged
        if early_stop:
            stopper = EarlyStopper(cur_step=self.start_step, **early_stop)

        with tqdm.tqdm(train_data, initial=start_batch, total=batches, unit='batch',
                       dynamic_ncols=True, disable=not distr.is_global_main) as data_bar:
            for batch in data_bar:
                batch_count += 1
                take_step = (batch_count % self.grad_accum_interval) == 0

                with autocast(enabled=dtorch.fp16):
                    if self.n_gpus <= 1:  # if not dataparallel, then move
                        batch = batch.to(device)

                    x_mask = (batch.x_seqs != batch.pad_val).unsqueeze(1)
                    scores = self.model(src=batch.x_seqs, src_mask=x_mask,
                                        score=self.criterion.input_type)
                    loss = self.loss_func(scores=scores, labels=batch.ys,
                                          train_mode=True, take_step=take_step)

                if stopper and take_step:
                    stopper.step()
                # Log
                unsaved_state = True
                if self.opt.curr_step % log_interval == 0:
                    self.tbd.add_scalars('training', {'step_loss': loss,
                                                      'learn_rate': self.opt.curr_lr},
                                         self.opt.curr_step)

                progress_msg, is_check_pt = train_state.step(len(batch), loss)
                progress_msg += f', LR={self.opt.curr_lr:0.8f}'
                data_bar.set_postfix_str(progress_msg, refresh=False)
                del batch

                # Save checkpoint
                if is_check_pt:
                    train_loss = train_state.reset()
                    log.info(
                        f"Chkpt Train loss={train_loss:g}; Runs validation? {distr.is_global_main}")
                    if distr.is_global_main:
                        train_state.train_mode(False)
                        with torch.no_grad():
                            val_loss, val_scores = self.run_valid_epoch(val_data)
                            self.make_check_point(train_loss, val_loss=val_loss,
                                                  keep_models=keep_models)
                            if check_pt_callback:
                                check_pt_callback(model=self.model,
                                                  step=self.opt.curr_step,
                                                  train_loss=train_loss)
                        train_state.train_mode(True)

                        if stopper:
                            stopper.validation(val_loss)
                            if stopper.is_stop():
                                log.info(f"Stopping at {stopper.cur_step} because {stopper.by}"
                                         f" didnt improve over {stopper.patience} checkpoints")
                                early_stopped = True
                                break
                    unsaved_state = False
                    gc.collect()
                    distr.barrier()

        # End of training
        if unsaved_state and distr.is_global_main:
            train_loss = train_state.reset()
            train_state.train_mode(False)
            val_loss = self.run_valid_epoch(val_data)
            self.make_check_point(train_loss, val_loss=val_loss, keep_models=keep_models)

        distr.barrier()
        return early_stopped


if __name__ == '__main__':
    args = dict(src_vocab=8000, tgt_vocab=3, enc_layers=2, hid_size=128, ff_size=256, n_heads=2)
    model, args_2 = TransformerClassifier.make_model(**args)
    # if you are running this in pycharm, please set Working Dir=<rtg repo base dir> for run config
    dir = 'experiments/sample-exp'
    from rtg.exp import TranslationExperiment as Experiment

    exp = Experiment(work_dir=dir, read_only=True)
    model.train()
    data = exp.get_train_data(batch_size=256, steps=100)
    for batch in data:
        x_mask = (batch.x_seqs != batch.pad_val).unsqueeze(1)
        ys = torch.randint(low=0, high=args['n_classes'], size=(len(batch), 1))
        res = model(src=batch.x_seqs, tgt=ys, src_mask=x_mask)
        print(res)
