from unittest.mock import Mock, patch

import pytest
import requests

from src.ingestion.http_util import get_json


def _resp(status: int, json_data=None):
    """Build a fake requests.Response.

    For >=400 statuses, raise_for_status() raises an HTTPError carrying the
    response (so the status code is inspectable) — matching requests.
    """
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


@patch("src.ingestion.http_util.requests.get")
def test_get_json_success(mock_get):
    mock_get.return_value = _resp(200, {"jobs": [1, 2]})
    assert get_json("http://example.test") == {"jobs": [1, 2]}
    assert mock_get.call_count == 1


@patch("src.ingestion.http_util.time.sleep")  # don't actually wait
@patch("src.ingestion.http_util.requests.get")
def test_get_json_retries_on_5xx_then_succeeds(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(503), _resp(200, {"ok": True})]
    assert get_json("http://example.test", retries=2) == {"ok": True}
    assert mock_get.call_count == 2
    assert mock_sleep.call_count == 1


@patch("src.ingestion.http_util.time.sleep")
@patch("src.ingestion.http_util.requests.get")
def test_get_json_does_not_retry_on_4xx(mock_get, mock_sleep):
    mock_get.return_value = _resp(404)
    with pytest.raises(requests.HTTPError):
        get_json("http://example.test", retries=3)
    # 4xx is a hard error — exactly one attempt, no backoff sleep.
    assert mock_get.call_count == 1
    mock_sleep.assert_not_called()


@patch("src.ingestion.http_util.time.sleep")
@patch("src.ingestion.http_util.requests.get")
def test_get_json_exhausts_retries_then_raises(mock_get, mock_sleep):
    mock_get.side_effect = requests.ConnectionError("boom")
    with pytest.raises(requests.ConnectionError):
        get_json("http://example.test", retries=2)
    # initial attempt + 2 retries
    assert mock_get.call_count == 3
    assert mock_sleep.call_count == 2
