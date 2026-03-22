#!/usr/bin/python3

import requests

S = requests.Session()

URL = "https://commons.wikimedia.org/w/api.php"

PARAMS = {
    "action": "query",
    "cmtitle": "Category:Quality_images",
    # "cmlimit": "5",
    "cmtype": "file",
    "list": "categorymembers",
    "cmlimit": "500",
    # 'generator': 'categorymembers', 
    # 'generator': 'allpages', 
    # "gaplimit": "500",
    "format": "json"
}

count = 0 
while True: 
    print(count) 
    R = S.get(url=URL, params=PARAMS)
    j = R.json()

    with open(f'contents/{count:07d}.json', 'w') as f: 
        f.write(R.text) 

    if 'error' in j:
        raise Exception(j['error'])
    if 'warnings' in j:
        print(j['warnings'])

    if 'continue' in j and j['continue']['cmcontinue']: 
        PARAMS['cmcontinue'] = j['continue']['cmcontinue']
    else: 
        break

    # PAGES = DATA['query']['categorymembers']
    # if count == 4: break

    count += 1 
