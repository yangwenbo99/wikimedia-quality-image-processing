#!/usr/bin/python3
"""
Filter Wikimedia images based on extended metadata.

The script operates on JSON files produced by `query-detailed-meta.py`
and supports the following filters (see `README.md` for background):

- Licence filters:
  - `--only-cc0`   : keep images with CC0 or PD licences only.
  - `--only-cc-by` : keep images with CC‑BY or more permissive
                    licences (including CC0 and PD), but exclude
                    share‑alike licences.

- Content filters:
  - `--only-people` / `--no-people`:
      keep only images that appear to depict people, or only those
      that do not, based on keywords in `ObjectName` or
      `ImageDescription`.
  - `--include-non-photo`:
      by default, only images that appear to be photos are kept
      (determined from file format); this flag disables that filter.
  - `--empirical-rule-1`:
      apply an empirical rule to reduce near‑duplicate content.
      Within images uploaded by the same user and within 12 hours,
      we discard later images whose `ObjectName` or
      `ImageDescription` cannot be determined to be sufficiently
      different from an earlier kept image, according to a chosen
      similarity metric (see `--er1-similarity`).

Optional dependencies (only for the chosen metric):
  - clip: open-clip-torch, torch
  - llama: llama-cpp-python (and --er1-llama-model path to a GGUF model)
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ImageItem = Dict[str, Any]
ImageInfo = Dict[str, Any]


PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff"}

# Default CLIP threshold: different enough when cosine similarity <= this.
# A lower value means we require the texts to be more dissimilar.
DEFAULT_CLIP_THRESHOLD = 0.3

# LLaMA verdicts: only "Different" means we keep the image.
LLAMA_VERDICT_SAME = "same"
LLAMA_VERDICT_DIFFERENT = "different"
LLAMA_VERDICT_INCONCLUSIVE = "inconclusive"
LLAMA_MAX_RETRIES = 3


@dataclass
class EmpiricalRuleConfig:
    enabled: bool
    similarity: str
    threshold: float
    window: timedelta
    llama_model_path: Optional[Path] = None
    clip_threshold: Optional[float] = None  # used when similarity is "clip"


def load_items(dir_path: Path) -> List[ImageItem]:
    """Load all page items from JSON files in dir_path."""
    items: List[ImageItem] = []
    for p in sorted(dir_path.glob("*.json")):
        with p.open("r") as f:
            data = json.load(f)
        if isinstance(data, list):
            items.extend(data)
        else:
            items.append(data)
    return items


def first_imageinfo(item: ImageItem) -> Optional[ImageInfo]:
    """Return the first imageinfo entry for an item, if present."""
    infos = item.get("imageinfo")
    if not infos:
        return None
    if not isinstance(infos, list):
        return None
    if not infos:
        return None
    ii = infos[0]
    if not isinstance(ii, dict):
        return None
    return ii


def license_value(ii: ImageInfo) -> Optional[str]:
    """Get License value from one imageinfo entry (supports extmetadata)."""
    ext = ii.get("extmetadata") or {}
    lic = ext.get("License") or ii.get("License")
    if lic and isinstance(lic, dict):
        return lic.get("value")
    if isinstance(lic, str):
        return lic
    return None


def license_class(lic: Optional[str]) -> str:
    """
    Classify a licence string.

    Returns one of:
    - "pd_or_cc0"
    - "cc_by_or_better" (includes pd/cc0)
    - "other"
    """
    if lic is None:
        return "other"
    lic_l = lic.lower()
    if lic_l in ("pd", "cc0"):
        return "pd_or_cc0"
    if lic_l.startswith("cc-by") and not lic_l.startswith("cc-by-sa"):
        return "cc_by_or_better"
    if lic_l in ("pd-old", "pd-author", "pd-us", "pd-usgov"):
        return "pd_or_cc0"
    return "other"


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


def text_from_extmetadata(ii: ImageInfo) -> Tuple[Optional[str], Optional[str]]:
    """Extract ObjectName and ImageDescription as plain text."""
    ext = ii.get("extmetadata") or {}

    def get_value(key: str) -> Optional[str]:
        val = ext.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if not isinstance(val, str):
            return None
        # Unescape HTML and strip tags.
        s = html.unescape(val)
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+", " ", s)
        s = s.strip()
        return s or None

    obj = get_value("ObjectName")
    desc = get_value("ImageDescription")
    return obj, desc


def has_people(ii: ImageInfo) -> Optional[bool]:
    """
    Heuristic: determine whether image likely contains people.

    Returns:
    - True if text strongly suggests presence of people.
    - False if text seems to indicate no people.
    - None if inconclusive (no decision).
    """
    obj, desc = text_from_extmetadata(ii)
    text_parts: List[str] = []
    if obj is not None:
        text_parts.append(obj)
    if desc is not None:
        text_parts.append(desc)
    if not text_parts:
        return None
    text = (" ".join(text_parts)).lower()

    positive_keywords = [
        "portrait",
        "self-portrait",
        "man",
        "woman",
        "boy",
        "girl",
        "men",
        "women",
        "child",
        "children",
        "people",
        "person",
        "crowd",
        "human",
        "family",
        "bride",
        "groom",
    ]
    negative_keywords = [
        "no people",
        "without people",
        "uninhabited",
        "empty street",
    ]
    for kw in positive_keywords:
        if kw in text:
            return True
    for kw in negative_keywords:
        if kw in text:
            return False
    return None


def uploader_from_item(item: ImageItem, ii: ImageInfo) -> Optional[str]:
    """
    Extract uploader/user identifier.

    Prefer a user name parsed from `Artist` in extmetadata.  Fallback
    to `user` field if present.
    """
    ext = ii.get("extmetadata") or {}
    artist = ext.get("Artist")
    value: Optional[str]
    if isinstance(artist, dict):
        value = artist.get("value")
    elif isinstance(artist, str):
        value = artist
    else:
        value = None

    if isinstance(value, str):
        # Typical form:
        # <a href="//commons.wikimedia.org/wiki/User:Foo" ...>Foo</a>
        m = re.search(
            r"User:([^\"'<>|]+)", value, flags=re.IGNORECASE
        )
        if m:
            return m.group(1)
        # Otherwise, strip tags and use remaining text.
        tmp = html.unescape(value)
        tmp = re.sub(r"<[^>]+>", " ", tmp)
        tmp = re.sub(r"\s+", " ", tmp).strip()
        if tmp:
            return tmp

    user = item.get("user")
    if isinstance(user, str) and user:
        return user
    return None


def datetime_from_metadata(ii: ImageInfo) -> Optional[datetime]:
    """
    Extract a datetime from metadata.

    Prefer DateTime; fall back to DateTimeOriginal if necessary.
    """
    ext = ii.get("extmetadata") or {}

    def get_dt(key: str) -> Optional[str]:
        val = ext.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if not isinstance(val, str):
            return None
        val = val.strip()
        return val or None

    for key in ("DateTime", "DateTimeOriginal"):
        s = get_dt(key)
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


def levenshtein_distance(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current_row = [i]
        for j, cb in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            replace_cost = previous_row[j - 1] + (ca != cb)
            current_row.append(
                min(insert_cost, delete_cost, replace_cost)
            )
        previous_row = current_row
    return previous_row[-1]


def similarity_score(
    a: str,
    b: str,
    method: str,
    *,
    clip_model: Any = None,
    clip_tokenizer: Any = None,
) -> float:
    """
    Compute similarity score (higher = more similar).

    For `levenshtein`: 1 - (distance / max_len) in [0, 1].
    For `clip`: cosine similarity in [-1, 1] between text embeddings.
    For `llama`: not used; use llama_verdict_for_pair instead.
    """
    method_l = method.lower()
    if method_l == "levenshtein":
        if not a and not b:
            return 1.0
        dist = levenshtein_distance(a, b)
        max_len = max(len(a), len(b))
        if max_len == 0:
            return 1.0
        return 1.0 - (dist / float(max_len))
    if method_l == "clip":
        if clip_model is None or clip_tokenizer is None:
            raise ValueError(
                "CLIP model and tokenizer required for method 'clip'"
            )
        return _clip_similarity(a, b, clip_model, clip_tokenizer)
    if method_l == "llama":
        raise ValueError(
            "Use llama_verdict_for_pair for method 'llama', not "
            "similarity_score"
        )
    raise ValueError(f"Unknown similarity method: {method}")


def _load_clip_model() -> Tuple[Any, Any]:
    """Load CLIP model and tokenizer (text encoder only). Requires open_clip."""
    try:
        import open_clip
        import torch
    except ImportError as e:
        raise ImportError(
            "CLIP similarity requires open_clip and torch. "
            "Install with: pip install open-clip-torch torch"
        ) from e
    model, _, _ = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="openai"
    )
    tokenizer = open_clip.get_tokenizer("ViT-B-32")
    model.eval()
    return model, tokenizer


def _clip_similarity(
    a: str, b: str, model: Any, tokenizer: Any
) -> float:
    """
    Cosine similarity between CLIP text embeddings, in [-1, 1].
    Requires torch and open_clip.
    """
    import torch
    import torch.nn.functional as F
    with torch.no_grad():
        tokens_a = tokenizer([a])
        tokens_b = tokenizer([b])
        emb_a = model.encode_text(tokens_a)
        emb_b = model.encode_text(tokens_b)
        emb_a = F.normalize(emb_a, p=2, dim=-1)
        emb_b = F.normalize(emb_b, p=2, dim=-1)
        cos = (emb_a @ emb_b.T).item()
    return float(cos)


def _llama_verdict(
    text_a: str,
    text_b: str,
    client: Any,
) -> str:
    """
    Ask the LLM whether two descriptions refer to the same content.
    Returns one of "Same", "Different", "Inconclusive". Retries up to
    LLAMA_MAX_RETRIES times if the answer is not parseable.
    """
    prompt = (
        "Below are two image descriptions.\n\n"
        "Image 1 (title/description):\n"
        f"{text_a}\n\n"
        "Image 2 (title/description):\n"
        f"{text_b}\n\n"
        "Do these describe the same content? First explain your "
        "reasoning. Then, on the last line, write exactly one word "
        "from this set: Same, Different, Inconclusive.\n\n"
        "Analysis and answer:\n"
    )
    valid = {"same", "different", "inconclusive"}
    for _ in range(LLAMA_MAX_RETRIES):
        out = client.create_completion(
            prompt,
            max_tokens=128,
            echo=False,
        )
        text = (
            (out.get("choices") or [{}])[0]
            .get("text", "")
            .strip()
            .lower()
        )
        if not text:
            continue
        # Use the last non-empty line as the verdict line.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            continue
        last = lines[-1]
        word = last.split()[0] if last.split() else ""
        if word in valid:
            return word
    return LLAMA_VERDICT_INCONCLUSIVE


def llama_verdict_for_pair(
    text_a: str,
    text_b: str,
    client: Any,
) -> bool:
    """
    True if the LLM says the two descriptions are "Different" (i.e.
    we should keep the image). Same or Inconclusive => False.
    """
    verdict = _llama_verdict(text_a, text_b, client)
    return verdict == LLAMA_VERDICT_DIFFERENT


def texts_for_similarity(ii: ImageInfo) -> List[str]:
    """Return non-empty text fields for similarity comparison."""
    obj, desc = text_from_extmetadata(ii)
    texts: List[str] = []
    if obj is not None:
        texts.append(obj)
    if desc is not None:
        texts.append(desc)
    return texts


def texts_different_enough(
    texts_a: Sequence[str],
    texts_b: Sequence[str],
    method: str,
    threshold: float,
    *,
    clip_threshold: Optional[float] = None,
    clip_model: Any = None,
    clip_tokenizer: Any = None,
    llama_client: Any = None,
) -> bool:
    """
    Decide if two images are different enough by text.

    For levenshtein: if at least one pair has similarity <= threshold,
    we consider them different enough.
    For clip: if at least one pair has cosine similarity <=
    clip_threshold, we consider them different enough.
    For llama: if at least one pair gets verdict "Different".
    If there are no comparable fields, returns True (cannot prove
    they are the same).
    """
    if not texts_a or not texts_b:
        return True
    method_l = method.lower()
    use_threshold = (
        clip_threshold
        if method_l == "clip" and clip_threshold is not None
        else threshold
    )
    found_pair = False
    for ta in texts_a:
        for tb in texts_b:
            found_pair = True
            if method_l == "llama":
                if llama_client is None:
                    raise ValueError(
                        "LLaMA client required for method 'llama'"
                    )
                if llama_verdict_for_pair(ta, tb, llama_client):
                    return True
            else:
                score = similarity_score(
                    ta, tb, method,
                    clip_model=clip_model,
                    clip_tokenizer=clip_tokenizer,
                )
                if score <= use_threshold:
                    return True
    return not found_pair


def _empirical_rule_1_clients(
    cfg: EmpiricalRuleConfig,
) -> Tuple[Any, Any, Any, Optional[float]]:
    """
    Load CLIP or LLaMA client when needed. Returns
    (clip_model, clip_tokenizer, llama_client, clip_threshold).
    """
    clip_model: Any = None
    clip_tokenizer: Any = None
    llama_client: Any = None
    clip_threshold: Optional[float] = (
        cfg.clip_threshold
        if cfg.clip_threshold is not None
        else DEFAULT_CLIP_THRESHOLD
    )
    method_l = cfg.similarity.lower()
    if method_l == "clip":
        clip_model, clip_tokenizer = _load_clip_model()
    elif method_l == "llama":
        if cfg.llama_model_path is None:
            raise ValueError(
                "LLaMA model path required when --er1-similarity=llama; "
                "use --er1-llama-model"
            )
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise ImportError(
                "LLaMA requires llama-cpp-python. "
                "Install with: pip install llama-cpp-python"
            ) from e
        llama_client = Llama(model_path=str(cfg.llama_model_path), n_ctx=512)
    return clip_model, clip_tokenizer, llama_client, clip_threshold


def apply_empirical_rule_1(
    items: List[ImageItem],
    cfg: EmpiricalRuleConfig,
) -> List[ImageItem]:
    """
    Apply empirical rule 1 to reduce near-duplicate images.

    Operates only on items that already passed other filters.
    """
    if not cfg.enabled:
        return items

    clip_model, clip_tokenizer, llama_client, clip_threshold = (
        _empirical_rule_1_clients(cfg)
    )

    # Group by uploader id.
    groups: Dict[str, List[Tuple[ImageItem, ImageInfo, datetime]]] = {}
    for item in items:
        ii = first_imageinfo(item)
        if ii is None:
            continue
        user = uploader_from_item(item, ii)
        if user is None:
            continue
        dt = datetime_from_metadata(ii)
        if dt is None:
            continue
        groups.setdefault(user, []).append((item, ii, dt))

    kept: List[ImageItem] = []
    # Items without enough information to enter groups are kept as-is.
    untouched: List[ImageItem] = []
    items_with_key = {id(t[0]) for g in groups.values() for t in g}
    for item in items:
        if id(item) not in items_with_key:
            untouched.append(item)

    for user, triples in groups.items():
        # Sort by datetime.
        triples_sorted = sorted(triples, key=lambda x: x[2])
        kept_for_user: List[Tuple[ImageItem, ImageInfo, datetime]] = []
        for item, ii, dt in triples_sorted:
            texts_curr = texts_for_similarity(ii)
            if not kept_for_user:
                kept_for_user.append((item, ii, dt))
                kept.append(item)
                continue
            # Compare with previously kept images within the window.
            discard = False
            for prev_item, prev_ii, prev_dt in kept_for_user:
                delta = abs((dt - prev_dt).total_seconds())
                if delta > cfg.window.total_seconds():
                    continue
                texts_prev = texts_for_similarity(prev_ii)
                if not texts_different_enough(
                    texts_prev,
                    texts_curr,
                    cfg.similarity,
                    cfg.threshold,
                    clip_threshold=clip_threshold,
                    clip_model=clip_model,
                    clip_tokenizer=clip_tokenizer,
                    llama_client=llama_client,
                ):
                    discard = True
                    break
            if discard:
                continue
            kept_for_user.append((item, ii, dt))
            kept.append(item)

    kept.extend(untouched)
    return kept


def op_filter_photos(
    items: Iterable[ImageItem],
    include_non_photo: bool,
) -> List[ImageItem]:
    """Apply the photo/non-photo filter."""
    if include_non_photo:
        return list(items)
    kept: List[ImageItem] = []
    for item in items:
        ii = first_imageinfo(item)
        if ii is not None and is_photo(ii):
            kept.append(item)
    return kept


def op_filter_license_cc0(
    items: Iterable[ImageItem],
) -> List[ImageItem]:
    """Keep only PD / CC0 items."""
    kept: List[ImageItem] = []
    for item in items:
        ii = first_imageinfo(item)
        if ii is not None and license_class(license_value(ii)) == "pd_or_cc0":
            kept.append(item)
    return kept


def op_filter_license_cc_by(
    items: Iterable[ImageItem],
) -> List[ImageItem]:
    """Keep only CC-BY (or better, including PD/CC0)."""
    kept: List[ImageItem] = []
    for item in items:
        ii = first_imageinfo(item)
        if ii is None:
            continue
        cls = license_class(license_value(ii))
        if cls in ("pd_or_cc0", "cc_by_or_better"):
            kept.append(item)
    return kept


def op_filter_only_people(
    items: Iterable[ImageItem],
) -> List[ImageItem]:
    """Keep only items explicitly marked as containing people."""
    kept: List[ImageItem] = []
    for item in items:
        ii = first_imageinfo(item)
        if ii is not None and has_people(ii) is True:
            kept.append(item)
    return kept


def op_filter_no_people(
    items: Iterable[ImageItem],
) -> List[ImageItem]:
    """Keep only items explicitly marked as not containing people."""
    kept: List[ImageItem] = []
    for item in items:
        ii = first_imageinfo(item)
        if ii is not None and has_people(ii) is False:
            kept.append(item)
    return kept


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Filter query-detailed-meta.py JSON outputs based on "
            "metadata."
        )
    )
    parser.add_argument(
        "dir",
        type=Path,
        help="Directory containing metadata JSON files",
    )
    parser.add_argument(
        "-O",
        "--output-json",
        type=Path,
        default=None,
        help=(
            "Write filtered items as JSON list to this file. "
            "If omitted, prints JSON to stdout."
        ),
    )
    lic_group = parser.add_mutually_exclusive_group()
    lic_group.add_argument(
        "--only-cc0",
        action="store_true",
        help="Keep only CC0 / PD images.",
    )
    lic_group.add_argument(
        "--only-cc-by",
        action="store_true",
        help=(
            "Keep only CC-BY or more permissive images "
            "(including PD and CC0)."
        ),
    )

    people_group = parser.add_mutually_exclusive_group()
    people_group.add_argument(
        "--only-people",
        action="store_true",
        help="Keep only images that likely contain people.",
    )
    people_group.add_argument(
        "--no-people",
        action="store_true",
        help="Keep only images that likely do not contain people.",
    )

    parser.add_argument(
        "--include-non-photo",
        action="store_true",
        help="Include images that are unlikely to be photos.",
    )

    parser.add_argument(
        "--empirical-rule-1",
        action="store_true",
        help="Apply empirical rule 1 to reduce near-duplicates.",
    )
    parser.add_argument(
        "--er1-similarity",
        choices=["levenshtein", "clip", "llama"],
        default="levenshtein",
        help=(
            "Similarity metric for empirical rule 1 "
            "(default: levenshtein)."
        ),
    )
    parser.add_argument(
        "--er1-threshold",
        type=float,
        default=0.5,
        help=(
            "For levenshtein: different enough if similarity <= this "
            "(default: 0.5). Ignored for clip/llama."
        ),
    )
    parser.add_argument(
        "--er1-clip-threshold",
        type=float,
        default=DEFAULT_CLIP_THRESHOLD,
        help=(
            "For clip: different enough if (cosine+1)/2 <= this "
            f"(default: {DEFAULT_CLIP_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--er1-llama-model",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Path to GGUF model for --er1-similarity=llama (required "
            "when using llama)."
        ),
    )
    parser.add_argument(
        "--er1-window-hours",
        type=float,
        default=12.0,
        help=(
            "Time window in hours for empirical rule 1 "
            "(default: 12)."
        ),
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    argv = sys.argv[1:]
    config = parser.parse_args(argv)

    dir_path = config.dir
    if not dir_path.is_dir():
        raise SystemExit(f"Not a directory: {dir_path}")

    items = load_items(dir_path)

    # If any filter is enabled, discard items without imageinfo early.
    any_filter_enabled = any(
        [
            config.only_cc0,
            config.only_cc_by,
            config.only_people,
            config.no_people,
            config.empirical_rule_1,
            not config.include_non_photo,
        ]
    )
    if any_filter_enabled:
        items = [
            item for item in items if first_imageinfo(item) is not None
        ]

    # Build operation sequence in the order the flags appear on CLI.
    # Skip the first non-option argument (the directory).
    ops: List[str] = []
    seen_dir = False
    for tok in argv:
        if not seen_dir and not tok.startswith("-"):
            seen_dir = True
            continue
        if not tok.startswith("-"):
            continue
        if tok in ("--only-cc0", "--only-cc-by",
                   "--only-people", "--no-people",
                   "--include-non-photo", "--empirical-rule-1"):
            ops.append(tok)

    # Default behaviour: photo filter first, unless user explicitly
    # disabled it with --include-non-photo (in which case we still
    # record that option but it becomes a no-op).
    current: List[ImageItem] = items

    # If user never mentioned include-non-photo, apply photo filter
    # implicitly before any other operations.
    if "--include-non-photo" not in ops:
        current = op_filter_photos(
            current,
            include_non_photo=False,
        )

    for op in ops:
        if op == "--include-non-photo":
            # This merely disables the default photo filter.  Since
            # we already respected the presence of this flag by
            # skipping the implicit photo-filter step above, we can
            # treat it as a no-op here.
            continue
        if op == "--only-cc0" and config.only_cc0:
            current = op_filter_license_cc0(current)
        elif op == "--only-cc-by" and config.only_cc_by:
            current = op_filter_license_cc_by(current)
        elif op == "--only-people" and config.only_people:
            current = op_filter_only_people(current)
        elif op == "--no-people" and config.no_people:
            current = op_filter_no_people(current)
        elif op == "--empirical-rule-1" and config.empirical_rule_1:
            if (
                config.er1_similarity == "llama"
                and config.er1_llama_model is None
            ):
                raise SystemExit(
                    "When using --er1-similarity=llama you must set "
                    "--er1-llama-model to a GGUF model path."
                )
            empirical_cfg = EmpiricalRuleConfig(
                enabled=True,
                similarity=config.er1_similarity,
                threshold=float(config.er1_threshold),
                window=timedelta(
                    hours=float(config.er1_window_hours)
                ),
                llama_model_path=config.er1_llama_model,
                clip_threshold=(
                    float(config.er1_clip_threshold)
                    if config.er1_similarity == "clip"
                    else None
                ),
            )
            current = apply_empirical_rule_1(current, empirical_cfg)

    out_obj: List[ImageItem] = current
    if config.output_json is None:
        print(json.dumps(out_obj, indent=2))
    else:
        config.output_json.parent.mkdir(parents=True, exist_ok=True)
        with config.output_json.open("w") as f:
            json.dump(out_obj, f, indent=2)


if __name__ == "__main__":
    main()

