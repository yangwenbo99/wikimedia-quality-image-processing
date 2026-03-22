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

def query_one_image(name: str): 
    PARAMS = {
        "action": "query",
        'prop': 'imageinfo', 
        'iiprop': 'url', 
        "format": "json", 
    }

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
    return url




def main(config): 
    # download_one_page(Path('../contents/contents/0000000.json'), Path('./res'))
    
    fid, qid = config.query.split('-')
    json_path = Path(config.source) / (fid + '.json')
    with open(json_path, 'r') as f: 
        j = json.load(f) 
    print(j['query']['categorymembers'][int(qid)]) 
    print(query_one_image(j['query']['categorymembers'][int(qid)]['title']))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Query wikimedia contents from TOC')
    parser.add_argument('-s','--source', help='source dir', required=True)
    parser.add_argument('-q','--query', help='query str', required=True)
    config = parser.parse_args()
    main(config)


