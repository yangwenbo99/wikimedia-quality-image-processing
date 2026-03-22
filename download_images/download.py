#!/usr/bin/python3

import requests
import argparse
from pathlib import Path
import json
import os
import sys
import time

class Waiter: 
    def __init__(self): 
        self.time = 0 

    def reset(self): 
        self.time = 0 

    def wait(self): 
        time.sleep(2 ** self.time)
        self.time += 1 


WAITER = Waiter()


S = requests.Session()

URL = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
        'User-Agent': "commons-downloader/1.0 (https://sr.ht/~nytpu/commons-downloader/)", 
        'Acccept': "jpg,jpeg,png,gif,tiff,tif,webp,webm,mp4"
        }
S.headers.update(HEADERS)

def download_one_image(name: str, dest: Path):
    PARAMS = {
        "action": "query",
        'prop': 'imageinfo', 
        'iiprop': 'url', 
        "format": "json", 
    }

    dest = Path(dest)
    PARAMS['titles'] = name

    R = S.get(url=URL, params=PARAMS)
    j = R.json()

    if 'error' in j:
        raise Exception(j['error'])
    if 'warnings' in j:
        print(j['warnings'])


    res = list(j['query']['pages'].values())[0]
    assert res['title'] == name
    url = res['imageinfo'][0]['url']
    suffix = Path(url).suffix
    print('   ', url, R.status_code)

    img = S.get(url) 
    with open(dest.with_suffix(suffix), 'wb') as f: 
        f.write(img.content)


def download_one_page(json_path: Path, dest_dir: Path, existing_stems=[]): 
    prefix = json_path.stem
    with open(json_path, 'r') as f: 
        j = json.load(f) 
    for idx, item in enumerate(j['query']['categorymembers']): 
        print(f'Page {prefix}, item {idx:03d}')
        title = item['title'] 
        stem = f'{prefix}-{idx:03d}'
        if stem in existing_stems: 
            continue

        loc = dest_dir / stem

        try: 
            download_one_image(title, loc)
            WAITER.reset()
        except Exception as e: 
            print(e, file=sys.stderr) 
            WAITER.wait()



def main(config): 
    # download_one_page(Path('../contents/contents/0000000.json'), Path('./res'))
    dest_path = Path(config.dest) 

    existing = set(x.stem for x in dest_path.iterdir() 
                   if os.path.getsize(x) > 3e5)
    for json_path in sorted(list(Path(config.source).iterdir())):
        download_one_page(json_path, dest_path, existing)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Download wikimedia contents from TOC')
    parser.add_argument('-s','--source', help='source dir', required=True)
    parser.add_argument('-d','--dest', help='destination dir, i.e. where files should be stored', required=True)
    config = parser.parse_args()
    main(config)

