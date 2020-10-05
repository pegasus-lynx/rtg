# What is this: USC ISI Coral team's MT pipeline
# Authors:
#      - Thamme Gowda
# Created : Oct 20, 2020

FROM nvidia/cuda:10.2-devel-ubuntu18.04

# Install curl, python3.7 and pip
RUN apt-get update && apt-get install -y \
    curl \
    python3.7 \
    python3-pip \
    python3.7-dev \
    git \
    && apt-get autoremove --purge

# Update pip
RUN python3.7 -m pip install --upgrade pip  && ln -s /usr/bin/python3.7 /usr/bin/python

#Make non-root user;
RUN useradd --create-home rtguser
#RUN chown -Rv rtguser:rtguser /home/rtguser
WORKDIR /home/rtguser
USER rtguser

# pip installed bins go here, they needs to be in PATH
RUN mkdir -p /home/rtguser/.local/bin
ENV CUDA_HOME="/usr/local/cuda-10.2/"
ENV PATH="/home/rtguser/.local/bin:/usr/local/cuda-10.2/bin:${PATH}"


# Install torch/torchvision and dependencies via pip
RUN pip install --user torch==1.6 rtg=0.5.0 gdown flask==1.1.2 && pip cache purge
# gdown is used for downloading large files from google drive

# Setup an experiment, get parent model
RUN cd /home/rtguser/ && \
   gdown https://drive.google.com/uc?id=1VpM-NIaGxLnAGaR17hQwyfWQH0oh5Knw -O rtgv0.5-768d9L6L-512K64K-datav1.tgz && \
   tar xvf rtgv0.5-768d9L6L-512K64K-datav1.tgz --one-top-level=rtgv0.5-768d9L6L-512K64K-datav1 --strip-components 1 && \
   rm rtgv0.5-768d9L6L-512K64K-datav1.tgz

CMD rtg-serve  /home/rtguser/rtgv0.5-768d9L6L-512K64K-datav1
