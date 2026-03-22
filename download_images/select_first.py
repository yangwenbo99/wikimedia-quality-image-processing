#!/usr/bin/python3

import argparse
from pathlib import Path
import PIL
from PIL import Image
import json
import os
import sys
import time
import multiprocessing


def main(config: argparse.Namespace):
    files = sorted(list(Path(config.source).iterdir()))[0:config.number] 
    dest_dir = Path(config.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    for file in files:
        dest_file = dest_dir / file.name
        dest_file.symlink_to(file.absolute())
    

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Select first n images from the downloaded images')
    parser.add_argument('-s','--source', help='source dir', required=True)
    parser.add_argument('-d','--dest', help='destination dir, i.e. where files should be stored', required=True)
    parser.add_argument('-n', '--number', help='number of files', type=int, default=20000)
    config = parser.parse_args()
    main(config)



