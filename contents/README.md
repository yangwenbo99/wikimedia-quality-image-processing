Wikimedia Commons Category Contents
===================================

This directory contains tools and data for collecting paginated
category listings from Wikimedia Commons. The JSON files produced here
are used as inputs to the image download and preparation pipeline in
`../download_images/`.


Script: `grab_contents.py`
--------------------------

- **Purpose**:  
  Page through a Wikimedia Commons category (by default
  `Category:Quality_images`) and save the raw API responses as JSON
  shards on disk.

- **How it works**
  - Uses the `list=categorymembers` API to request files from the
    configured category.
  - Requests up to 500 entries per call (`cmlimit=500`).
  - Follows the `cmcontinue` token returned by the API to fetch the
    next page of results until there are no more pages.
  - After each request, writes the raw JSON response text to
    `contents/<NNNNNNN>.json`, where `<NNNNNNN>` is a zero‑padded
    counter starting from `0000000`.

- **Output layout**
  - All JSON files are written into a `contents/` subdirectory next to
    `grab_contents.py`.
  - Each JSON file has the general structure:
    - `query.categorymembers[*].title`: the file titles that are later
      used by the download scripts.
    - `continue.cmcontinue`: continuation token for the next page (if
      present).

- **Running the script**

  The script is currently configured entirely via the constant
  `PARAMS` in the source code and has no command‑line interface.

  Typical usage:

  ```bash
  cd wikimedia/contents
  python3 grab_contents.py
  ```

  After it finishes, you should see JSON files such as:

  ```text
  contents/0000000.json
  contents/0000001.json
  contents/0000002.json
  ...
  ```

  These files can then be consumed by `download.py` and `query.py` in
  `../download_images/`.


Dependencies
------------

- Python 3
- `requests`

Install the Python dependency with:

```bash
pip install requests
```

