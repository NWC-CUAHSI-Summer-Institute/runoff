"""Per-user local paths, loaded from config.yaml at the repo root."""

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / 'config.yaml'
if not _CONFIG_PATH.exists():
    _CONFIG_PATH = _REPO_ROOT / 'config.yaml.example'

with open(_CONFIG_PATH) as f:
    _config = yaml.safe_load(f)

HYDROFABRIC_GPKG = Path(_config['hydrofabric_gpkg'])
EVENTS_CSV = Path(_config['events_csv'])
CACHE_DIR = Path(_config['cache_dir'])
STUDY_START = str(_config['study_start'])
STUDY_END = str(_config['study_end'])
