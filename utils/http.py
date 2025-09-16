# utils/http.py
# Basic HTTP helpers with requests

from __future__ import annotations
import requests
from typing import Any, Dict, Optional

def get_json(url: str, timeout: int = 10, headers: Optional[Dict[str, str]] = None) -> Any:
    """GET request που επιστρέφει JSON (ή σηκώνει exception)."""
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.json()

def post_json(url: str, payload: Dict[str, Any], timeout: int = 10, headers: Optional[Dict[str, str]] = None) -> tuple[int, str]:
    """POST request με JSON body. Επιστρέφει (status_code, text)."""
    r = requests.post(url, json=payload, timeout=timeout, headers=headers)
    return r.status_code, r.text
