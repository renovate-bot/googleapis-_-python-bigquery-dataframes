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

"""Clients manages the connection to Google APIs."""

import os
import typing
from typing import Optional
import warnings

import google.api_core.client_info
import google.api_core.client_options
import google.api_core.exceptions
import google.api_core.gapic_v1.client_info
import google.auth.credentials
import google.cloud.bigquery as bigquery
import google.cloud.bigquery_connection_v1
import google.cloud.bigquery_storage_v1
import google.cloud.functions_v2
import google.cloud.resourcemanager_v3
import pydata_google_auth

import bigframes.constants
import bigframes.exceptions as bfe
import bigframes.version

_ENV_DEFAULT_PROJECT = "GOOGLE_CLOUD_PROJECT"
_APPLICATION_NAME = f"bigframes/{bigframes.version.__version__} ibis/9.2.0"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


# BigQuery is a REST API, which requires the protocol as part of the URL.
_BIGQUERY_LOCATIONAL_ENDPOINT = "https://{location}-bigquery.googleapis.com"
_BIGQUERY_REGIONAL_ENDPOINT = "https://bigquery.{location}.rep.googleapis.com"

# BigQuery Connection and Storage are gRPC APIs, which don't support the
# https:// protocol in the API endpoint URL.
_BIGQUERYCONNECTION_LOCATIONAL_ENDPOINT = "{location}-bigqueryconnection.googleapis.com"
_BIGQUERYSTORAGE_LOCATIONAL_ENDPOINT = "{location}-bigquerystorage.googleapis.com"
_BIGQUERYSTORAGE_REGIONAL_ENDPOINT = (
    "https://bigquerystorage.{location}.rep.googleapis.com"
)


def _get_default_credentials_with_project():
    return pydata_google_auth.default(scopes=_SCOPES, use_local_webserver=False)


class ClientsProvider:
    """Provides client instances necessary to perform cloud operations."""

    def __init__(
        self,
        project: Optional[str] = None,
        location: Optional[str] = None,
        use_regional_endpoints: Optional[bool] = None,
        credentials: Optional[google.auth.credentials.Credentials] = None,
        application_name: Optional[str] = None,
        bq_kms_key_name: Optional[str] = None,
        client_endpoints_override: dict = {},
    ):
        credentials_project = None
        if credentials is None:
            credentials, credentials_project = _get_default_credentials_with_project()

        # Prefer the project in this order:
        # 1. Project explicitly specified by the user
        # 2. Project set in the environment
        # 3. Project associated with the default credentials
        project = (
            project
            or os.getenv(_ENV_DEFAULT_PROJECT)
            or typing.cast(Optional[str], credentials_project)
        )

        if not project:
            raise ValueError(
                "Project must be set to initialize BigQuery client. "
                "Try setting `bigframes.options.bigquery.project` first."
            )

        self._application_name = (
            f"{_APPLICATION_NAME} {application_name}"
            if application_name
            else _APPLICATION_NAME
        )
        self._project = project

        if (
            use_regional_endpoints
            and location is not None
            and location.lower()
            not in bigframes.constants.REP_ENABLED_BIGQUERY_LOCATIONS
        ):
            msg = bfe.format_message(
                bigframes.constants.LEP_DEPRECATION_WARNING_MESSAGE.format(
                    location=location
                ),
                fill=False,
            )
            warnings.warn(msg, category=FutureWarning)
        self._location = location
        self._use_regional_endpoints = use_regional_endpoints

        self._credentials = credentials
        self._bq_kms_key_name = bq_kms_key_name
        self._client_endpoints_override = client_endpoints_override

        # cloud clients initialized for lazy load
        self._bqclient = None
        self._bqconnectionclient: Optional[
            google.cloud.bigquery_connection_v1.ConnectionServiceClient
        ] = None
        self._bqstoragereadclient: Optional[
            google.cloud.bigquery_storage_v1.BigQueryReadClient
        ] = None
        self._cloudfunctionsclient: Optional[
            google.cloud.functions_v2.FunctionServiceClient
        ] = None
        self._resourcemanagerclient: Optional[
            google.cloud.resourcemanager_v3.ProjectsClient
        ] = None

    def _create_bigquery_client(self):
        bq_options = None
        if "bqclient" in self._client_endpoints_override:
            bq_options = google.api_core.client_options.ClientOptions(
                api_endpoint=self._client_endpoints_override["bqclient"]
            )
        elif self._use_regional_endpoints:
            endpoint_template = _BIGQUERY_REGIONAL_ENDPOINT
            if (
                self._location is not None
                and self._location.lower()
                not in bigframes.constants.REP_ENABLED_BIGQUERY_LOCATIONS
            ):
                endpoint_template = _BIGQUERY_LOCATIONAL_ENDPOINT

            bq_options = google.api_core.client_options.ClientOptions(
                api_endpoint=endpoint_template.format(location=self._location)
            )

        bq_info = google.api_core.client_info.ClientInfo(
            user_agent=self._application_name
        )

        bq_client = bigquery.Client(
            client_info=bq_info,
            client_options=bq_options,
            credentials=self._credentials,
            project=self._project,
            location=self._location,
        )
        if self._bq_kms_key_name:
            # Note: Key configuration only applies automatically to load and query jobs, not copy jobs.
            encryption_config = bigquery.EncryptionConfiguration(
                kms_key_name=self._bq_kms_key_name
            )
            default_load_job_config = bigquery.LoadJobConfig()
            default_query_job_config = bigquery.QueryJobConfig()
            default_load_job_config.destination_encryption_configuration = (
                encryption_config
            )
            default_query_job_config.destination_encryption_configuration = (
                encryption_config
            )
            bq_client.default_load_job_config = default_load_job_config
            bq_client.default_query_job_config = default_query_job_config

        return bq_client

    @property
    def bqclient(self):
        if not self._bqclient:
            self._bqclient = self._create_bigquery_client()

        return self._bqclient

    @property
    def bqconnectionclient(self):
        if not self._bqconnectionclient:
            bqconnection_options = None
            if "bqconnectionclient" in self._client_endpoints_override:
                bqconnection_options = google.api_core.client_options.ClientOptions(
                    api_endpoint=self._client_endpoints_override["bqconnectionclient"]
                )
            elif self._use_regional_endpoints:
                bqconnection_options = google.api_core.client_options.ClientOptions(
                    api_endpoint=_BIGQUERYCONNECTION_LOCATIONAL_ENDPOINT.format(
                        location=self._location
                    )
                )

            bqconnection_info = google.api_core.gapic_v1.client_info.ClientInfo(
                user_agent=self._application_name
            )
            self._bqconnectionclient = (
                google.cloud.bigquery_connection_v1.ConnectionServiceClient(
                    client_info=bqconnection_info,
                    client_options=bqconnection_options,
                    credentials=self._credentials,
                )
            )

        return self._bqconnectionclient

    @property
    def bqstoragereadclient(self):
        if not self._bqstoragereadclient:
            bqstorage_options = None
            if "bqstoragereadclient" in self._client_endpoints_override:
                bqstorage_options = google.api_core.client_options.ClientOptions(
                    api_endpoint=self._client_endpoints_override["bqstoragereadclient"]
                )
            elif self._use_regional_endpoints:
                endpoint_template = _BIGQUERYSTORAGE_REGIONAL_ENDPOINT
                if (
                    self._location is not None
                    and self._location.lower()
                    not in bigframes.constants.REP_ENABLED_BIGQUERY_LOCATIONS
                ):
                    endpoint_template = _BIGQUERYSTORAGE_LOCATIONAL_ENDPOINT

                bqstorage_options = google.api_core.client_options.ClientOptions(
                    api_endpoint=endpoint_template.format(location=self._location)
                )

            bqstorage_info = google.api_core.gapic_v1.client_info.ClientInfo(
                user_agent=self._application_name
            )
            self._bqstoragereadclient = (
                google.cloud.bigquery_storage_v1.BigQueryReadClient(
                    client_info=bqstorage_info,
                    client_options=bqstorage_options,
                    credentials=self._credentials,
                )
            )

        return self._bqstoragereadclient

    @property
    def cloudfunctionsclient(self):
        if not self._cloudfunctionsclient:
            functions_info = google.api_core.gapic_v1.client_info.ClientInfo(
                user_agent=self._application_name
            )
            self._cloudfunctionsclient = (
                google.cloud.functions_v2.FunctionServiceClient(
                    client_info=functions_info,
                    credentials=self._credentials,
                )
            )

        return self._cloudfunctionsclient

    @property
    def resourcemanagerclient(self):
        if not self._resourcemanagerclient:
            resourcemanager_info = google.api_core.gapic_v1.client_info.ClientInfo(
                user_agent=self._application_name
            )
            self._resourcemanagerclient = (
                google.cloud.resourcemanager_v3.ProjectsClient(
                    credentials=self._credentials, client_info=resourcemanager_info
                )
            )

        return self._resourcemanagerclient
