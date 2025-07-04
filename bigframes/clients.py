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

"""BigQuery DataFrame clients to interact with other cloud resources"""

from __future__ import annotations

import logging
import textwrap
import time
from typing import cast, Optional

import google.api_core.exceptions
import google.api_core.retry
from google.cloud import bigquery_connection_v1, resourcemanager_v3
from google.iam.v1 import policy_pb2

logger = logging.getLogger(__name__)


def get_canonical_bq_connection_id(
    connection_id: str, default_project: str, default_location: str
) -> str:
    """
    Retrieve the full connection id of the form
    <PROJECT_NUMBER/PROJECT_ID>.<LOCATION>.<CONNECTION_ID>.
    Use default project, location or connection_id when any of them are missing.
    """

    if "/" in connection_id:
        fields = connection_id.split("/")
        if (
            len(fields) == 6
            and fields[0] == "projects"
            and fields[2] == "locations"
            and fields[4] == "connections"
        ):
            return ".".join((fields[1], fields[3], fields[5]))
    else:
        if connection_id.count(".") == 2:
            return connection_id

        if connection_id.count(".") == 1:
            return f"{default_project}.{connection_id}"

        if connection_id.count(".") == 0:
            return f"{default_project}.{default_location}.{connection_id}"

    raise ValueError(
        textwrap.dedent(
            f"""
        Invalid connection id format: {connection_id}.
        Only the following formats are supported:
            <project-id>.<location>.<connection-id>,
            <location>.<connection-id>,
            <connection-id>,
            projects/<project-id>/locations/<location>/connections/<connection-id>
        """
        ).strip()
    )


class BqConnectionManager:
    """Manager to handle operations with BQ connections."""

    # Wait time (in seconds) for an IAM binding to take effect after creation
    _IAM_WAIT_SECONDS = 120

    def __init__(
        self,
        bq_connection_client: bigquery_connection_v1.ConnectionServiceClient,
        cloud_resource_manager_client: resourcemanager_v3.ProjectsClient,
    ):
        self._bq_connection_client = bq_connection_client
        self._cloud_resource_manager_client = cloud_resource_manager_client

    def create_bq_connection(
        self,
        project_id: str,
        location: str,
        connection_id: str,
        iam_role: Optional[str] = None,
    ):
        """Create the BQ connection if not exist. In addition, try to add the IAM role to the connection to ensure required permissions.

        Args:
            project_id:
                ID of the project.
            location:
                Location of the connection.
            connection_id:
                ID of the connection.
            iam_role:
                str of the IAM role that the service account of the created connection needs to aquire. E.g. 'run.invoker', 'aiplatform.user'
        """
        # If the intended connection does not exist then create it
        service_account_id = self._get_service_account_if_connection_exists(
            project_id, location, connection_id
        )
        if service_account_id:
            logger.info(
                f"BQ connection {project_id}.{location}.{connection_id} already exists"
            )
        else:
            connection_name, service_account_id = self._create_bq_connection(
                project_id, location, connection_id
            )
            logger.info(
                f"Created BQ connection {connection_name} with service account id: {service_account_id}"
            )
        service_account_id = cast(str, service_account_id)

        # Ensure IAM role on the BQ connection
        # https://cloud.google.com/bigquery/docs/reference/standard-sql/remote-functions#grant_permission_on_function
        if iam_role:
            try:
                self._ensure_iam_binding(project_id, service_account_id, iam_role)
            except google.api_core.exceptions.PermissionDenied as ex:
                ex.message = f"Failed ensuring IAM binding (role={iam_role}, service-account={service_account_id}). {ex.message}"
                raise

    # Introduce retries to accommodate transient errors like:
    # (1) Etag mismatch,
    #     which can be caused by concurrent operation on the same resource, and
    #     manifests with message like:
    #     google.api_core.exceptions.Aborted: 409 There were concurrent policy
    #     changes. Please retry the whole read-modify-write with exponential
    #     backoff. The request's ETag '\007\006\003,\264\304\337\272' did not
    #     match the current policy's ETag '\007\006\003,\3750&\363'.
    # (2) Connection creation,
    #     for which sometimes it takes a bit for its service account to reflect
    #     across APIs (e.g. b/397662004, b/386838767), before which, an attempt
    #     to set an IAM policy for the service account may throw an error like:
    #     google.api_core.exceptions.InvalidArgument: 400 Service account
    #     bqcx-*@gcp-sa-bigquery-condel.iam.gserviceaccount.com does not exist.
    @google.api_core.retry.Retry(
        predicate=google.api_core.retry.if_exception_type(
            google.api_core.exceptions.Aborted,
            google.api_core.exceptions.InvalidArgument,
        ),
        initial=10,
        maximum=20,
        multiplier=2,
        timeout=60,
    )
    def _ensure_iam_binding(
        self, project_id: str, service_account_id: str, iam_role: str
    ):
        """Ensure necessary IAM role is configured on a service account."""
        project = f"projects/{project_id}"
        service_account = f"serviceAccount:{service_account_id}"
        role = f"roles/{iam_role}"
        request = {
            "resource": project
        }  # Use a dictionary to avoid problematic google.iam namespace package.
        policy = self._cloud_resource_manager_client.get_iam_policy(request=request)

        # Check if the binding already exists, and if does, do nothing more
        for binding in policy.bindings:
            if binding.role == role:
                if service_account in binding.members:
                    return

        # Create a new binding
        new_binding = policy_pb2.Binding(role=role, members=[service_account])
        policy.bindings.append(new_binding)
        request = {
            "resource": project,
            "policy": policy,
        }  # Use a dictionary to avoid problematic google.iam namespace package.
        self._cloud_resource_manager_client.set_iam_policy(request=request)

        # We would wait for the IAM policy change to take effect
        # https://cloud.google.com/iam/docs/access-change-propagation
        logger.info(
            f"Waiting {self._IAM_WAIT_SECONDS} seconds for IAM to take effect.."
        )
        time.sleep(self._IAM_WAIT_SECONDS)

    def _create_bq_connection(self, project_id: str, location: str, connection_id: str):
        """Create the BigQuery Connection and returns corresponding service account id."""
        client = self._bq_connection_client
        connection = bigquery_connection_v1.Connection(
            cloud_resource=bigquery_connection_v1.CloudResourceProperties()
        )
        request = bigquery_connection_v1.CreateConnectionRequest(
            parent=client.common_location_path(project_id, location),
            connection_id=connection_id,
            connection=connection,
        )
        connection = client.create_connection(request)
        return connection.name, connection.cloud_resource.service_account_id

    def _get_service_account_if_connection_exists(
        self, project_id: str, location: str, connection_id: str
    ) -> Optional[str]:
        """Check if the BigQuery Connection exists."""
        client = self._bq_connection_client
        request = bigquery_connection_v1.GetConnectionRequest(
            name=client.connection_path(project_id, location, connection_id)
        )

        service_account = None
        try:
            service_account = client.get_connection(
                request=request
            ).cloud_resource.service_account_id
        except google.api_core.exceptions.NotFound:
            pass

        return service_account
