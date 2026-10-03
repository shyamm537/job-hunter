from unittest.mock import Mock, patch

import pytest
import requests

from src.ingestion.http_util import REQUEST_DELAY, USER_AGENT, get_json, post_json


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


@patch("src.ingestion.http_util.requests.get")
def test_get_json_sends_the_user_agent_and_any_extra_headers(mock_get):
    mock_get.return_value = _resp(200, {})
    get_json("http://example.test", headers={"Accept": "application/json"})
    sent = mock_get.call_args.kwargs["headers"]
    assert sent == {"User-Agent": USER_AGENT, "Accept": "application/json"}

    mock_get.reset_mock()
    get_json("http://example.test")  # no extra headers: just the User-Agent
    assert mock_get.call_args.kwargs["headers"] == {"User-Agent": USER_AGENT}


def test_request_delay_matches_the_link_checker_pause():
    from src.ingestion.check_links import DELAY

    assert REQUEST_DELAY == DELAY == 0.5


# --- post_json ---------------------------------------------------------------


@patch("src.ingestion.http_util.requests.post")
def test_post_json_success_sends_the_body_as_json_with_headers(mock_post):
    mock_post.return_value = _resp(200, {"total": 3, "jobPostings": []})
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

    out = post_json("http://example.test", body, headers={"Accept": "application/json"})

    assert out == {"total": 3, "jobPostings": []}
    assert mock_post.call_count == 1
    assert mock_post.call_args.args == ("http://example.test",)
    assert mock_post.call_args.kwargs["json"] == body
    assert mock_post.call_args.kwargs["headers"] == {
        "User-Agent": USER_AGENT, "Accept": "application/json",
    }


@patch("src.ingestion.http_util.time.sleep")
@patch("src.ingestion.http_util.requests.post")
def test_post_json_retries_on_5xx_then_succeeds(mock_post, mock_sleep):
    mock_post.side_effect = [_resp(503), _resp(200, {"ok": True})]
    assert post_json("http://example.test", {}, retries=2) == {"ok": True}
    assert mock_post.call_count == 2
    assert mock_sleep.call_count == 1


@patch("src.ingestion.http_util.time.sleep")
@patch("src.ingestion.http_util.requests.post")
@pytest.mark.parametrize("status", [400, 404, 422])
def test_post_json_does_not_retry_on_4xx(mock_post, mock_sleep, status):
    # Workday answers 400 (page size too big), 404 (wrong site), 422 (no such
    # tenant): all hard errors, one attempt each.
    mock_post.return_value = _resp(status)
    with pytest.raises(requests.HTTPError):
        post_json("http://example.test", {}, retries=3)
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("src.ingestion.http_util.time.sleep")
@patch("src.ingestion.http_util.requests.post")
def test_post_json_exhausts_retries_then_raises(mock_post, mock_sleep):
    mock_post.side_effect = requests.Timeout("slow")
    with pytest.raises(requests.Timeout):
        post_json("http://example.test", {}, retries=2)
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2
