#!/usr/bin/python3

import requests
import argparse
from pathlib import Path
import json
import os
import sys
import time
from typing import List, Dict, Any

MAX_RETRY = 5
MAX_NUM_IMAGE_EACH_QUERY = 25

class Waiter: 
    def __init__(self): 
        self.time = 0 

    def reset(self): 
        self.time = 0 

    def wait(self, time_to_sleep=-1): 
        if time_to_sleep >= 0: 
            time_to_sleep = max(time_to_sleep, 2 ** self.time)
            time.sleep(time_to_sleep)
        else:
            time.sleep(2 ** self.time)
        self.time += 1 


WAITER = Waiter()


S = requests.Session()

URL = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
        'User-Agent': "commons-downloader/1.0 (https://sr.ht/~nytpu/commons-downloader/)", 
        'Acccept': "jpg,jpeg,png,gif,tiff,tif,webp,webm,mp4,json"
        }
S.headers.update(HEADERS)

def query_images(names: List[str]) -> List[Dict[str, Any]]:
    params = {
        "action": "query",
        "prop": "imageinfo",
        "iiprop": "extmetadata|url",
        "format": "json",
        "titles": "|".join(names),
    }

    r = S.get(url=URL, params=params)
    if r.status_code in [429, 414]:
        r.raise_for_status()
    try:
        j = r.json()
    except Exception as e:
        raise Exception(
            f"Failed to parse JSON response: {e}, response text: {r.text}"
        )

    if "error" in j:
        raise Exception(j["error"])
    if "warnings" in j:
        print(j["warnings"])

    pages = j["query"]["pages"].values()
    return list(pages)


def query_batch(
    names: List[str],
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """Query a batch, subdividing on 414 URI Too Long."""
    for _ in range(MAX_RETRY):
        try:
            result = query_images(names)
            WAITER.reset()
            return result
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 414:
                if len(names) <= 1:
                    print(
                        "414 even with a single title,"
                        f" skipping: {names}",
                        file=sys.stderr,
                    )
                    return []
                mid = len(names) // 2
                if verbose:
                    print(
                        f"414 with {len(names)} titles,"
                        " subdividing into"
                        f" {mid} + {len(names) - mid}..."
                    )
                left = query_batch(names[:mid], verbose)
                right = query_batch(
                    names[mid:], verbose
                )
                return left + right
            elif e.response.status_code == 429:
                print(
                    "429 Too Many Requests,"
                    " waiting before retrying...",
                    file=sys.stderr,
                )
                WAITER.wait(10)
            else:
                print(
                    f"HTTP {e.response.status_code}:"
                    f" {e}",
                    file=sys.stderr,
                )
                WAITER.wait()
        except Exception as e:
            print(
                f"Error querying batch: {e}",
                file=sys.stderr,
            )
            WAITER.wait()
    print(
        f"Failed after {MAX_RETRY} retries for"
        f" {len(names)} titles, skipping.",
        file=sys.stderr,
    )
    return []


def main(config):
    source_dir_path = Path(config.source)
    dest_dir_path = Path(config.destination)
    dest_dir_path.mkdir(parents=True, exist_ok=True)
    for source_json_path in sorted(
        source_dir_path.glob("*.json")
    ):
        dest_path = dest_dir_path / source_json_path.name
        if dest_path.exists():
            print(
                f"{dest_path} already exists, skipping."
            )
            continue
        print(f"Processing {source_json_path}...")
        res_list = []
        with open(source_json_path, "r") as f:
            contents = json.load(f)

        members = contents["query"]["categorymembers"]
        titles = [m["title"] for m in members]

        for i in range(
            0, len(titles), MAX_NUM_IMAGE_EACH_QUERY
        ):
            batch = titles[
                i : i + MAX_NUM_IMAGE_EACH_QUERY
            ]
            if config.verbose:
                print(
                    "Querying batch of images "
                    f"{i} - {i + len(batch) - 1} "
                    f"out of {len(titles)}..."
                )
            res_list.extend(
                query_batch(batch, config.verbose)
            )

        dest_json_path = (
            dest_dir_path / source_json_path.name
        )
        with open(dest_json_path, "w") as f:
            json.dump(res_list, f, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Query wikimedia contents' extended metadata from the "
            "existing contents json files"
        )
    )
    parser.add_argument("-s", "--source", help="source dir", required=True)
    parser.add_argument(
        "-d",
        "--destination",
        help="destination dir to save the query results",
        required=True,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print verbose logs",
    )
    config = parser.parse_args()
    main(config)


