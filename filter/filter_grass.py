#!/usr/bin/env python

'''filter_grass.py: Filter out images with grass
'''

import argparse
import json
from typing import List, Dict, Union
from pathlib import Path


def filter_from_list(src_data: List[Dict[str, Union[str, int]]]):
    res = []
    for i, item in enumerate(src_data['query']['categorymembers']):
        title = item['title'].lower().split()
        if 'grass' in title or (
                ('football' in title or 'soccer' in title) # and
                # ('field' in title or 'pitch' in title)
                ):
            item['idx'] = i
            res.append(item)
    return res


def main(args):
    res = []
    for i, file in enumerate(sorted(Path(args.input_dir).glob('*.json'))):
        with open(file, 'r') as f:
            data = json.load(f)
        filtered_data = filter_from_list(data)
        for item in filtered_data:
            item['page'] = i
        res.extend(filtered_data)
        print(f'Processed {file}, {len(filtered_data)} items found')

    with open(args.output, 'w') as f:
        json.dump(res, f, indent=4)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input_dir', type=str)
    parser.add_argument('output', type=str)
    args = parser.parse_args()
    main(args)


