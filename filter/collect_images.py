#!/usr/bin/env python

'''collect_images.py: Collect images from a JSON list as produced by filter_*.py
'''

import argparse
import json
from typing import List, Dict, Union
from pathlib import Path
import tarfile
import shutil
import os
from glob import glob

def copy_image(
        page_idx: int, img_idx: int,
        tars_dir: Path, 
        src_img_dir: Path,
        dst_img_dir: Path
        ):
    '''
    There are some questionable choices in 
    '''
    # 0000000-003.*
    candidates = glob(f'{src_img_dir}/{page_idx:07d}-{img_idx:03d}.*')
    assert len(candidates) <= 1
    if len(candidates) == 0:
        # If the source image not found, extract it from corresponding tar file
        # archive_0000606.tar
        tar_file = tars_dir / f'archive_{page_idx:07d}.tar'
        tar = tarfile.open(tar_file, 'r')
        members = tar.getmembers()
        for member in members:
            if f'{page_idx:07d}-{img_idx:03d}' in member.name:
                tar.extract(member, path=src_img_dir)
                break
        src_img_path = src_img_dir / Path(member.name).name
    else:
        src_img_path = Path(candidates[0])
    dst_img_path = dst_img_dir / src_img_path.name

    # Check whether hardlink is possible, if not, copy the file
    try:
        os.link(src_img_path, dst_img_path)
    except FileExistsError or shutil.SameFileError:
        # The file is already linked
        pass
    except OSError:
        shutil.copy(src_img_path, dst_img_path)


def main(args):
    with open(args.input, 'r') as f:
        data = json.load(f)

    tars_dir = Path(args.tars_dir)
    src_img_dir = Path(args.src_img_dir)
    dst_img_dir = Path(args.dst_img_dir)
    dst_img_dir.mkdir(parents=True, exist_ok=True)

    for item in data:
        print(f'Copying image {item["page"]}-{item["idx"]}')
        copy_image(
            item['page'], item['idx'],
            tars_dir, src_img_dir, dst_img_dir
            )

    print(f'{len(data)} images copied to {dst_img_dir}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
            '-i', '--input', type=str, required=True, 
            help='Input list file as JSON')
    parser.add_argument(
            '-t', '--tars_dir', type=str, required=True,
            help='Directory containing tar files')
    parser.add_argument(
            '-s', '--src_img_dir', type=str, required=True,
            help='Directory containing images')
    parser.add_argument(
            '-d', '--dst_img_dir', type=str, required=True,
            help='Directory to copy images to')
    args = parser.parse_args()
    main(args)
    


'''
python collect_images.py \
        -i '/mnt/datamonster/data/experiments_datasets/wikimedia/contents/contents_grass.json' \
        -t '/mnt/datamonster/data/wikimedia/wikimedia_splits' \
        -s '/mnt/datamonster/data/wikimedia/wikimedia_splits_extracted' \
        -d '/mnt/datamonster/data/wikimedia/wikimedia_parts_by_topics/grass' 


'''




