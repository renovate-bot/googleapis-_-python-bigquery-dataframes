# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest.mock as mock

import bigframes_vendored.constants as constants
import google.api_core.exceptions as api_core_exceptions
import google.cloud.bigquery as bigquery
import pytest

import bigframes.formatting_helpers as formatting_helpers
import bigframes.version


def test_wait_for_query_job_error_includes_feedback_link():
    mock_query_job = mock.create_autospec(bigquery.QueryJob)
    mock_query_job.result.side_effect = api_core_exceptions.BadRequest(
        "Test message 123."
    )

    with pytest.raises(api_core_exceptions.BadRequest) as cap_exc:
        formatting_helpers.wait_for_query_job(mock_query_job)

    cap_exc.match("Test message 123.")
    cap_exc.match(constants.FEEDBACK_LINK)


def test_wait_for_job_error_includes_feedback_link():
    mock_job = mock.create_autospec(bigquery.LoadJob)
    mock_job.result.side_effect = api_core_exceptions.BadRequest("Test message 123.")

    with pytest.raises(api_core_exceptions.BadRequest) as cap_exc:
        formatting_helpers.wait_for_job(mock_job)

    cap_exc.match("Test message 123.")
    cap_exc.match(constants.FEEDBACK_LINK)


def test_wait_for_job_error_includes_version():
    mock_job = mock.create_autospec(bigquery.LoadJob)
    mock_job.result.side_effect = api_core_exceptions.BadRequest("Test message 123.")

    with pytest.raises(api_core_exceptions.BadRequest) as cap_exc:
        formatting_helpers.wait_for_job(mock_job)

    cap_exc.match("Test message 123.")
    cap_exc.match(bigframes.version.__version__)


@pytest.mark.parametrize(
    "test_input, expected", [(None, "N/A"), ("string", "N/A"), (100000, "100.0 kB")]
)
def test_get_formatted_bytes(test_input, expected):
    assert formatting_helpers.get_formatted_bytes(test_input) == expected


@pytest.mark.parametrize(
    "test_input, expected", [(None, None), ("string", "string"), (100000, "a minute")]
)
def test_get_formatted_time(test_input, expected):
    assert formatting_helpers.get_formatted_time(test_input) == expected
