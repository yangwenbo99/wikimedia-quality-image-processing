#!/usr/bin/env python3

import argparse
import html
import json
import re
import sys
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

"""
Filter Wikimedia images based on extended metadata.

Usage: 
    ./meta-filter.py \
            input_json_dir \
            -o output_json_path \
            [include-non-photo] \
            [all other filters...]

The script operates on JSON files produced by `query-detailed-meta.py`
and supports the following filters (see `README.md` for background):

- Licence filters:
  - `only-cc0`   : keep images with CC0 or PD licences only.
  - `only-cc-by` : keep images with CC‑BY or more permissive
                    licences (including CC0 and PD), but exclude
                    share‑alike licences.

- Content filters:
  - `only-people` / `no-people`:
      keep only images that appear to depict people, or only those
      that do not.  By default a keyword heuristic on `ObjectName`
      and `ImageDescription` is used.
      Options:
        - -m, --method: "keyword" (default) or "llama"
        - -R, --repo-id: HuggingFace repo for -m llama
            default: "Qwen/Qwen3-4B-GGUF"
        - -M, --model: GGUF model filename for -m llama
            default: "Qwen3-4B-Q4_K_M.gguf"
  - `include-non-photo`:
      by default, only images that appear to be photos are kept
      (determined from file format); this flag disables that filter.
  - `empirical-rule-1`:
      apply an empirical rule to reduce near‑duplicate content.
      Within images uploaded by the same user and within 12 hours,
      we discard later images whose `ObjectName` or
      `ImageDescription` cannot be determined to be sufficiently
      different from an earlier kept image, according to a chosen
      similarity metric (see `--er1-similarity`).
      Options for empirical rule follow this argument:
        - -m, --method: the method to be used for the rule, choose from
            ["levenshtein", "clip", "llama"]
        - -t, --threshold: the threshold to accept the pair
        - -R, --repo-id: determine the model to use for -m llama, 
            default: "Qwen/Qwen3-4B-GGUF"
        - -M, --model: determine the model to use for -m llama
            default: "Qwen3-4B-Q4_K_M.gguf"
        - -w, --window-hours: the time windoe for two images to be consider
            possible duplicates. 

Optional dependencies (only for the chosen metric):
  - clip: open-clip-torch, torch
  - llama: llama-cpp-python

Example:
    ./meta-filter.py \
            input_json_dir \
            -O output_json_path \
            only-cc0 \
            only-people \
            empirical-rule-1 \


llama can be downloaded using the following command: 
"""

VERBOSE = False
ALLOWED_OPS = [
        'only-cc0',
        'only-cc-by',
        'only-people',
        'no-people',
        'include-non-photo',
        'empirical-rule-1',
        ]
METHODS = ['levenshtein', 'clip', 'llama']
PEOPLE_METHODS = ['keyword', 'llama']
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff"}
_VERBOSE_MAX_EXAMPLES = 5

ImageInfo = Dict[str, Any]

_llama_model = None


def _license_value(ii: Dict[str, Any]) -> Optional[str]:
    """Get Licence value from one imageinfo entry (supports extmetadata)."""
    ext = ii.get("extmetadata") or {}
    lic = ext.get("License") or ii.get("License")
    if lic and isinstance(lic, dict):
        return lic.get("value")
    return None

def filter_license(input_list: List[dict], license_: str) -> List[dict]:
    '''Filter based on licence.

    Parameters
    ----------
    input_list:
        List of page dictionaries as returned by ``query-detailed-meta``.
    license_:
        Either ``"only-cc0"`` or ``"only-cc-by"``.
    '''
    if license_ not in ('only-cc0', 'only-cc-by'):
        raise ValueError(
            f"license_ must be 'only-cc0' or 'only-cc-by', got {license_!r}"
        )

    def _keep_license(lic: str) -> bool:
        if license_ == 'only-cc0':
            return lic in {'pd', 'cc0'}
        # license_ == 'only-cc-by'
        if lic in {'pd', 'cc0'}:
            return True
        if lic.startswith('cc-by') and not lic.startswith('cc-by-sa'):
            return True
        return False

    kept: List[dict] = []
    for item in input_list:
        imageinfo_list = item.get('imageinfo') or []
        if not imageinfo_list:
            continue
        assert len(imageinfo_list) <= 1
        lic_val = _license_value(imageinfo_list[0])
        if lic_val is None:
            continue
        if _keep_license(lic_val):
            kept.append(item)
    return kept


_PEOPLE_KEYWORDS = [
    "person",
    "people",
    "man",
    "woman",
    "boy",
    "girl",
    "child",
    "children",
    "human",
    # "portrait",
    "self-portrait",
    "crowd",
    "men",
    "women",
    "family",
    "bride",
    "groom",
    "pedestrian",
    "walker",
    "cyclist",
    "runner",
    "swimmer",
    "dancer",
    "athlete",
]

_PEOPLE_PATTERN = re.compile(
    r"\b(?:" + "|".join(
        re.escape(kw) for kw in _PEOPLE_KEYWORDS
    ) + r")\b"
)


def _item_has_people_keyword(item: Dict[str, Any]) -> Union[bool, None]:
    """Heuristic keyword-based people detector for one page item.

    This is deliberately a separate helper so that alternative
    detection strategies (e.g. model-based) can be added later
    without changing the public ``filter_people`` API.
    """
    imageinfo_list = item.get("imageinfo") or []
    if not imageinfo_list:
        return False

    # For now, assume at most one imageinfo entry as in other code.
    ii = imageinfo_list[0]
    ext = ii.get("extmetadata") or {}

    texts: List[str] = []

    obj = ext.get("ObjectName") or ii.get("ObjectName")
    if isinstance(obj, dict):
        val = obj.get("value")
        if isinstance(val, str):
            texts.append(val)

    desc = ext.get("ImageDescription") or ii.get("ImageDescription")
    if isinstance(desc, dict):
        val = desc.get("value")
        if isinstance(val, str):
            texts.append(val)

    if not texts:
        return False

    combined = " ".join(texts).lower()
    if _PEOPLE_PATTERN.search(combined):
        return True
    return False


def _item_has_people_llama(
    item: Dict[str, Any],
) -> Optional[bool]:
    """LLM-based people detector using Llama.

    Sends the filename, ObjectName, and ImageDescription
    to the model and asks whether the image depicts people.
    Returns True, False, or None (when metadata is absent
    or the answer is ambiguous).
    """
    assert _llama_model is not None
    llm = _llama_model

    imageinfo_list = item.get("imageinfo") or []
    if not imageinfo_list:
        return None

    ii = imageinfo_list[0]
    obj = _ext_text_value(ii, "ObjectName")
    desc = _ext_text_value(ii, "ImageDescription")
    title = item.get("title") or ""

    if obj is None and desc is None and not title:
        return None

    title_str = title or "(no filename)"
    obj_str = obj or "(no object name)"
    desc_str = desc or "(no description)"

    prompt = (
        "> A Wikimedia Commons image has"
        f' filename "{title_str}",'
        f' object name "{obj_str}", and'
        f' description "{desc_str}".'
        " Based on these metadata fields,"
        " does this image likely depict or"
        " contain any people (humans)?"
        "\n\nBased on the metadata,"
    )
    stops = ["\n\n", "\n>"]
    output = llm(
        prompt,
        max_tokens=256,
        stop=stops,
        echo=True,
        temperature=0.7,
        top_p=0.8,
        top_k=20,
        min_p=0,
    )
    reasoning = (
        output["choices"][0]["text"].rstrip("\n")
    )

    output = llm(
        reasoning + "\n"
        '\n> Give a one-word answer ("yes"'
        ' or "no") to the question "does'
        " this image depict or contain"
        ' people?"'
        '\n\n"',
        max_tokens=2,
        echo=True,
    )

    full = output["choices"][0]["text"]
    marker = '\n\n"'
    idx = full.rfind(marker)
    if idx >= 0:
        tail = full[idx + len(marker):]
    else:
        tail = ""
    tail = tail.lower().strip().rstrip('"')
    if "yes" in tail:
        return True
    if "no" in tail:
        return False
    return None


def filter_people(
    input_list: List[dict],
    has_people: Optional[bool],
    method: str = "keyword",
    **llama_config,
) -> List[dict]:
    """Filter items based on whether they depict people.

    Parameters
    ----------
    input_list:
        List of page dicts from ``query-detailed-meta``.
    has_people:
        - True  : keep items depicting people.
        - False : keep items *not* depicting people.
        - None  : return input unmodified.
    method:
        ``"keyword"`` (regex heuristic) or ``"llama"``
        (LLM-based).  When ``"llama"`` is chosen the
        model must already be initialised via
        ``_init_llama`` or the caller must pass
        ``repo_id`` and ``model`` in *llama_config*.
    **llama_config:
        repo_id : str
            HuggingFace repo id (llama only).
        model : str
            GGUF model filename (llama only).
    """
    if has_people is None:
        return input_list

    if method not in PEOPLE_METHODS:
        raise ValueError(
            f"method must be one of {PEOPLE_METHODS},"
            f" got {method!r}"
        )

    if method == "llama":
        _init_llama(
            llama_config.get(
                "repo_id",
                "Qwen/Qwen3-4B-GGUF",
            ),
            llama_config.get(
                "model",
                "Qwen3-4B-Q4_K_M.gguf",
            ),
        )
        detect_fn = _item_has_people_llama
    else:
        detect_fn = _item_has_people_keyword

    kept: List[dict] = []
    for item in input_list:
        people_flag = detect_fn(item)
        if has_people and people_flag:
            kept.append(item)
        elif not has_people and not people_flag:
            kept.append(item)
    return kept


def is_photo(ii: ImageInfo) -> bool:
    """
    Heuristic: treat JPEG/TIFF images as photos.

    This follows the observation that digital artworks are unlikely to
    be saved as JPEG/TIFF when labelled as "Quality Image" on Commons.
    """
    url = ii.get("url")
    if not isinstance(url, str):
        return False
    url_l = url.lower()
    for ext in PHOTO_EXTENSIONS:
        if url_l.endswith(ext):
            return True
    return False

def filter_photos(input_list: List[dict]) -> List[dict]:
    """Filter items to keep only those that appear to be photos based on 
    file extension. 
    """
    res_list: List[dict] = []
    for item in input_list:
        imageinfo_list = item.get("imageinfo") or []
        if not imageinfo_list:
            continue
        # For now, assume at most one imageinfo entry as in other code.
        ii = imageinfo_list[0]
        if is_photo(ii):
            res_list.append(item)
    return res_list



def _ext_text_value(
    ii: ImageInfo, key: str,
) -> Optional[str]:
    """Extract a plain-text value from extmetadata."""
    ext = ii.get("extmetadata") or {}
    val = ext.get(key)
    if isinstance(val, dict):
        val = val.get("value")
    if not isinstance(val, str):
        return None
    s = html.unescape(val)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _uploader_from_item(
    item: Dict[str, Any], ii: ImageInfo,
) -> Optional[str]:
    """Extract uploader/user identifier.

    Prefer a user name parsed from ``Artist`` in extmetadata;
    fall back to the top-level ``user`` field.
    """
    ext = ii.get("extmetadata") or {}
    artist = ext.get("Artist")
    value: Optional[str] = None
    if isinstance(artist, dict):
        value = artist.get("value")
    elif isinstance(artist, str):
        value = artist

    if isinstance(value, str):
        m = re.search(
            r"User:([^\"'<>|]+)", value,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1)
        tmp = html.unescape(value)
        tmp = re.sub(r"<[^>]+>", " ", tmp)
        tmp = re.sub(r"\s+", " ", tmp).strip()
        if tmp:
            return tmp

    user = item.get("user")
    if isinstance(user, str) and user:
        return user
    return None


def _datetime_from_metadata(
    ii: ImageInfo,
) -> Optional[datetime]:
    """Extract a datetime from metadata fields.

    Prefer ``DateTime``; fall back to ``DateTimeOriginal``.
    """
    ext = ii.get("extmetadata") or {}

    def _get_str(key: str) -> Optional[str]:
        val = ext.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if not isinstance(val, str):
            return None
        return val.strip() or None

    for key in ("DateTime", "DateTimeOriginal"):
        s = _get_str(key)
        if not s:
            continue
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _levenshtein_distance(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            curr.append(min(
                curr[j - 1] + 1,
                prev[j] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = curr
    return prev[-1]


def _levenshtein_similarity(a: str, b: str) -> float:
    """Normalized Levenshtein similarity in [0, 1].

    Computed as ``1 - distance / max(len(a), len(b))``.
    """
    if not a and not b:
        return 1.0
    max_len = max(len(a), len(b))
    return 1.0 - _levenshtein_distance(a, b) / max_len


def _different_enough_levenshtein(
    ii_a: ImageInfo,
    ii_b: ImageInfo,
    threshold: float,
) -> Tuple[bool, dict]:
    """Determine whether two images are different enough.

    Returns ``(different_enough, detail)`` where *detail*
    contains the similarity scores for the fields compared.

    *different_enough* is True when either of the following
    holds:

    * Both have ``ObjectName`` and the Levenshtein similarity
      between them is at most *threshold*.
    * Both have ``ImageDescription`` and the Levenshtein
      similarity between them is at most *threshold*.
    """
    obj_a = _ext_text_value(ii_a, "ObjectName")
    obj_b = _ext_text_value(ii_b, "ObjectName")
    desc_a = _ext_text_value(ii_a, "ImageDescription")
    desc_b = _ext_text_value(ii_b, "ImageDescription")

    detail: dict = {
        "obj_sim": None, "desc_sim": None,
    }
    different = False

    if obj_a is not None and obj_b is not None:
        sim = _levenshtein_similarity(obj_a, obj_b)
        detail["obj_sim"] = sim
        if sim <= threshold:
            different = True
    if desc_a is not None and desc_b is not None:
        sim = _levenshtein_similarity(desc_a, desc_b)
        detail["desc_sim"] = sim
        if sim <= threshold:
            different = True

    return different, detail


def _init_llama(
    repo_id: str, model_file: str,
) -> None:
    """Lazily initialize the Llama model."""
    global _llama_model
    if _llama_model is not None:
        return
    from llama_cpp import Llama
    _llama_model = Llama.from_pretrained(
        repo_id=repo_id,
        filename=model_file,
        n_gpu_layers=-1,
        n_ctx=4096,
    )


def _llama_check_similarity(
    desc_a: Optional[str],
    obj_a: Optional[str],
    desc_b: Optional[str],
    obj_b: Optional[str],
) -> str:
    """Chain-of-thought prompting for similarity.

    Returns ``"yes"``, ``"no"``, or ``"unknown"``.
    """
    assert _llama_model is not None
    llm = _llama_model

    da = desc_a or "(no description)"
    oa = obj_a or "(no object name)"
    db = desc_b or "(no description)"
    ob = obj_b or "(no object name)"

    stops = ["\n\n", "\n>"]

    prompt = (
        "> There are two images from Wikimedia"
        " Common.  Image A has the description"
        f' "{da}", and "object name" "{oa}".'
        "  Image B has the description"
        f' "{db}" and "object name" {ob}.'
        "  You need to analyze whether these"
        " two images contain substantially"
        " similar contents.  Similar contents"
        " means the same object(s), similar"
        " looking objects, or different parts"
        " of the same object, even if they are"
        " captured from different angles."
        "\n\nImage A is likely"
    )
    output = llm(
        prompt,
        max_tokens=1024,
        stop=stops,
        echo=True,
        temperature=0.7,
        top_p=0.8,
        top_k=20,
        min_p=0,
        presence_penalty=1.5,
    )
    last = (
        output["choices"][0]["text"]
        .rstrip("\n")
    )

    if "image b is" not in last.lower():
        output = llm(
            last + "On the other hand,"
            " Image B is likely showing",
            max_tokens=512,
            stop=stops,
            echo=True,
        )
        last = (
            output["choices"][0]["text"]
            .rstrip("\n")
        )

    if "therefore," not in last.lower():
        output = llm(
            last + "Therefore,",
            max_tokens=512,
            stop=stops + ["."],
            echo=True,
        )
        last = (
            output["choices"][0]["text"]
            .rstrip("\n")
        )

    if not last.endswith("."):
        last += "."

    output = llm(
        last + "\n"
        "\n> If have to give a one-word answer"
        ' ("yes", "no" or "unknown") to the'
        ' the question "whether the contents'
        ' in Image A and B are the same".'
        " \n\n\"",
        max_tokens=2,
        echo=True,
    )

    full = output["choices"][0]["text"]
    marker = "\n\n\""
    idx = full.rfind(marker)
    if idx >= 0:
        tail = full[idx + len(marker):]
    else:
        tail = ""
    tail = tail.lower().strip().rstrip('"')
    for token in ("yes", "no", "unknown"):
        if token in tail:
            return token
    return "unknown"


def _different_enough_llama(
    ii_a: ImageInfo,
    ii_b: ImageInfo,
    threshold: float,
) -> Tuple[bool, dict]:
    """Check whether two images differ using LLM.

    Maps the LLM answer to a similarity score:
    yes → 1.0, unknown → 0.5, no → 0.0.
    The pair is different enough when the score
    is at most *threshold*.
    """
    desc_a = _ext_text_value(
        ii_a, "ImageDescription",
    )
    desc_b = _ext_text_value(
        ii_b, "ImageDescription",
    )
    obj_a = _ext_text_value(ii_a, "ObjectName")
    obj_b = _ext_text_value(ii_b, "ObjectName")

    answer = _llama_check_similarity(
        desc_a, obj_a, desc_b, obj_b,
    )
    _SIM = {
        "yes": 1.0,
        "no": 0.0,
        "unknown": 0.5,
    }
    sim = _SIM.get(answer, 0.5)

    detail: dict = {
        "obj_sim": None,
        "desc_sim": None,
        "llama_answer": answer,
        "llama_sim": sim,
    }
    return sim <= threshold, detail


def _truncate(
    s: Optional[str], maxlen: int = 60,
) -> str:
    """Truncate *s* for display."""
    if s is None:
        return "<none>"
    if len(s) <= maxlen:
        return s
    return s[: maxlen - 3] + "..."


def _print_pair_examples(
    label: str,
    examples: List[Tuple[str, str, dict]],
) -> None:
    """Print verbose pair-comparison examples."""
    if not examples:
        return
    shown = examples[:_VERBOSE_MAX_EXAMPLES]
    total = len(examples)
    print(
        f"  {label} ({total} total,"
        f" showing {len(shown)}):"
    )
    for i, (title_a, title_b, d) in enumerate(
        shown, start=1,
    ):
        print(f"    {i}. {_truncate(title_a)}")
        print(f"       vs {_truncate(title_b)}")
        sims = []
        if d["obj_sim"] is not None:
            sims.append(
                f"obj={d['obj_sim']:.3f}"
            )
        if d["desc_sim"] is not None:
            sims.append(
                f"desc={d['desc_sim']:.3f}"
            )
        if d.get("llama_answer") is not None:
            sims.append(
                f"llama={d['llama_answer']}"
            )
        if sims:
            print(
                f"       similarity:"
                f" {', '.join(sims)}"
            )


def filter_empirical_rule_1(
    input_list: List[dict], **config,
) -> List[dict]:
    """Filter near-duplicate images using empirical rule 1.

    Parameters
    ----------
    input_list:
        List of page dicts from ``query-detailed-meta``.
    **config:
        method : str
            ``"levenshtein"``, ``"clip"``, or ``"llama"``.
        threshold : float
            Similarity threshold (pairs with similarity at
            most this value are considered different enough).
        window_hours : float
            Time window in hours.
        repo_id : str
            (llama only) HuggingFace repo id.
        model : str
            (llama only) GGUF model filename.
    """
    method = config["method"]
    if method not in METHODS:
        raise ValueError(
            f"Method must be one of {METHODS}, "
            f"got {method!r}"
        )
    threshold: float = config["threshold"]
    window = timedelta(hours=config["window_hours"])

    if method == "clip":
        raise NotImplementedError(
            "Method 'clip' is not yet implemented"
        )
    if method == "llama":
        _init_llama(
            config["repo_id"], config["model"],
        )

    if method == "levenshtein":
        _diff_fn = _different_enough_levenshtein
    else:
        _diff_fn = _different_enough_llama

    groups: Dict[
        str, List[Tuple[dict, ImageInfo, datetime]]
    ] = {}
    ungrouped: List[dict] = []

    for item in input_list:
        ii_list = item.get("imageinfo") or []
        if not ii_list:
            ungrouped.append(item)
            continue
        ii = ii_list[0]
        user = _uploader_from_item(item, ii)
        dt = _datetime_from_metadata(ii)
        if user is None or dt is None:
            ungrouped.append(item)
            continue
        groups.setdefault(user, []).append(
            (item, ii, dt)
        )

    kept: List[dict] = list(ungrouped)
    too_similar: List[
        Tuple[str, str, dict]
    ] = []
    diff_enough: List[
        Tuple[str, str, dict]
    ] = []

    window_secs = window.total_seconds()
    for _user, triples in groups.items():
        triples.sort(key=lambda x: x[2])
        kept_for_user: List[
            Tuple[dict, ImageInfo, datetime]
        ] = []
        win_start = 0
        for item, ii, dt in triples:
            if not kept_for_user:
                kept_for_user.append(
                    (item, ii, dt)
                )
                kept.append(item)
                continue
            # Advance win_start past kept items
            # whose timestamp is outside the window.
            while win_start < len(kept_for_user):
                _, _, ws_dt = (
                    kept_for_user[win_start]
                )
                if (
                    (dt - ws_dt).total_seconds()
                    <= window_secs
                ):
                    break
                win_start += 1
            discard = False
            for prev_item, prev_ii, prev_dt in (
                kept_for_user[win_start:]
            ):
                diff, detail = _diff_fn(
                    prev_ii, ii, threshold,
                )
                if not diff:
                    discard = True
                    if VERBOSE:
                        too_similar.append((
                            prev_item.get(
                                "title", "?"
                            ),
                            item.get(
                                "title", "?"
                            ),
                            detail,
                        ))
                    break
                if VERBOSE:
                    diff_enough.append((
                        prev_item.get(
                            "title", "?"
                        ),
                        item.get(
                            "title", "?"
                        ),
                        detail,
                    ))
            if not discard:
                kept_for_user.append(
                    (item, ii, dt)
                )
                kept.append(item)

    if VERBOSE:
        _print_pair_examples(
            "Pairs NOT different enough"
            " (discarded)",
            too_similar,
        )
        _print_pair_examples(
            "Pairs different enough (kept)",
            diff_enough,
        )

    return kept


def main():
    input_path = Path(sys.argv[1])
    if not input_path.exists():
        raise ValueError("Input path does not exist")
    if input_path.is_file():
        input_json_paths = [input_path]
    if input_path.is_dir():
        input_json_paths = list(sorted(p for p in input_path.iterdir() if p.is_file()))

    if sys.argv[2] != '-o':
        raise ValueError("The second option must be -o in order to specify an output path")
    output_path = Path(sys.argv[3])

    # Read the input metadata and annotate each item
    # with its on-disk stem (``{prefix}-{idx:03d}``,
    # matching the naming scheme of ``download.py``).
    input_list: List[dict] = []
    for input_json_path in input_json_paths:
        with open(input_json_path, 'r') as f:
            contents = json.load(f)
        prefix = input_json_path.stem
        for idx, item in enumerate(contents):
            item["_disk_stem"] = (
                f"{prefix}-{idx:03d}"
            )
        input_list += contents

    if 'include-non-photo' not in sys.argv[4:]:
        input_list = filter_photos(input_list)

    argi = 4
    while argi < len(sys.argv):
        if sys.argv[argi] == '-v':
            global VERBOSE
            VERBOSE = True
            argi += 1
        else:
            break

    while argi < len(sys.argv):
        op = sys.argv[argi]
        if op in ['only-cc0', 'only-cc-by']:
            input_list = filter_license(input_list, op)
        elif op in ['only-people', 'no-people']:
            people_method = "keyword"
            people_repo = "Qwen/Qwen3-4B-GGUF"
            people_model = (
                "Qwen3-4B-Q4_K_M.gguf"
            )
            while (argi + 1 < len(sys.argv) 
                   and (opop := sys.argv[argi + 1]).startswith('-')):
                if opop in ['-m', '--method']:
                    people_method = (
                        sys.argv[argi + 2]
                    )
                    if people_method not in (
                        PEOPLE_METHODS
                    ):
                        raise ValueError(
                            "People filter method"
                            " must be one of"
                            f" {PEOPLE_METHODS}"
                        )
                    argi += 2
                elif opop in [
                    '-R', '--repo-id',
                ]:
                    people_repo = (
                        sys.argv[argi + 2]
                    )
                    argi += 2
                elif opop in ['-M', '--model']:
                    people_model = (
                        sys.argv[argi + 2]
                    )
                    argi += 2
                else:
                    break
            input_list = filter_people(
                input_list,
                op == 'only-people',
                method=people_method,
                repo_id=people_repo,
                model=people_model,
            )
        elif op == 'empirical-rule-1':
            rule1_method = 'llama'
            rule1_threshold = 0.5
            rule1_repo = "Qwen/Qwen3-4B-GGUF"
            rule1_model = "Qwen3-4B-Q4_K_M.gguf"
            rule1_window = 12
            while argi + 1 < len(sys.argv) and (
                    opop := sys.argv[argi + 1]).startswith('-'):
                if opop in ['-m', '--method']:
                    rule1_method = sys.argv[argi + 2]
                    if rule1_method not in METHODS:
                        raise ValueError(f"Rule 1's method must be {METHODS}")
                    argi += 2
                elif opop in ['-t', '--threshold']:
                    rule1_threshold = float(sys.argv[argi + 2])
                    argi += 2
                elif opop in ['-R', '--repo-id']:
                    rule1_repo = sys.argv[argi + 2]
                    argi += 2
                elif opop in ['-M', '--method']:
                    rule1_repo = sys.argv[argi + 2]
                    argi += 2
                elif opop in ['-w', '--window-hours']:
                    rule1_window = float[argi + 2]
                    argi += 2
                else:
                    raise ValueError(f"Unknow option for empirical-rule-1: {opop}")
            input_list = filter_empirical_rule_1(
                input_list,
                method=rule1_method,
                threshold=rule1_threshold,
                repo_id=rule1_repo,
                model=rule1_model,
                window_hours=rule1_window,
            )
        else:
            raise ValueError(f"Unknown operation {op}")

        if VERBOSE:
            print(f"After {op} filter, {len(input_list)} items remain.")

        argi += 1
    
    with open(output_path, 'w') as f:
        json.dump(input_list, f, indent=2)




if __name__ == '__main__':
    main()

