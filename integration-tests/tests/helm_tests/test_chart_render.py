"""Render tests for the CLP package Helm chart."""

import re
from typing import Any

import pytest

from tests.helm_tests.utils import (
    HelmTemplateError,
    parse_properties,
    RELEASE_NAME,
    render_chart,
    RenderedChart,
)

pytestmark = pytest.mark.helm

COORDINATOR = f"{RELEASE_NAME}-presto-coordinator"
WORKER = f"{RELEASE_NAME}-presto-worker"
CATALOG_SECRET = f"{RELEASE_NAME}-presto-catalog"
CONFIG_MAP = f"{RELEASE_NAME}-config"
PRESTO_CONFIG_MAP = f"{RELEASE_NAME}-presto-config"
COORDINATOR_CATALOG = "coordinator-catalog-clp.properties"
WORKER_CATALOG = "worker-catalog-clp.properties"
PLUGIN_DIR = "/opt/presto-server/plugin/clp"
CONNECTOR_REPOSITORY = "ghcr.io/y-scope/clp-plugin-presto-connector"

STATIC_CREDENTIALS = {
    "access_key_id": "AKIAEXAMPLEKEY",
    "secret_access_key": "example/secret+access=key",
}


def _presto_values() -> dict[str, Any]:
    """:return: Values that deploy the bundled Presto, with archives on the filesystem."""
    return {
        "clpConfig": {"webui": {"query_engine": "presto"}},
        "clp-presto": {
            "enabled": True,
            "coordinator": {
                "loggingLevel": "INFO",
                "queryMaxMemoryGb": 1,
                "queryMaxMemoryPerNodeGb": 1,
            },
            "worker": {"queryMemoryGb": 4, "systemMemoryGb": 8},
        },
    }


def _s3_values(aws_authentication: dict[str, Any]) -> dict[str, Any]:
    """
    :param aws_authentication:
    :return: Values that deploy the bundled Presto, with archives on S3.
    """
    values = _presto_values()
    values["serviceAccount"] = {
        "annotations": {"eks.amazonaws.com/role-arn": "arn:aws:iam::<account-id>:role/<role-name>"},
    }
    values["clpConfig"]["archive_output"] = {
        "storage": {
            "type": "s3",
            "s3_config": {
                "region_code": "ca-central-1",
                "bucket": "example-bucket",
                "key_prefix": "archives/",
                "aws_authentication": aws_authentication,
            },
        },
    }
    # The subchart needs the storage type and region, which Helm cannot derive from `clpConfig`.
    values["clp-presto"]["archiveStorage"] = {
        "type": "s3",
        "s3": {"regionCode": "ca-central-1"},
    }
    return values


def _default_auth_values() -> dict[str, Any]:
    return _s3_values({"type": "default"})


def _static_auth_values() -> dict[str, Any]:
    return _s3_values({"type": "credentials", "credentials": dict(STATIC_CREDENTIALS)})


def _config_map_properties(chart: RenderedChart, key: str) -> dict[str, str]:
    return parse_properties(chart.get("ConfigMap", PRESTO_CONFIG_MAP)["data"][key])


def _catalog(chart: RenderedChart, key: str) -> dict[str, str]:
    return parse_properties(chart.get("Secret", CATALOG_SECRET)["stringData"][key])


@pytest.fixture(scope="module")
def presto_chart() -> RenderedChart:
    """:return: The chart rendered with the bundled Presto."""
    return render_chart(_presto_values())


@pytest.fixture(scope="module")
def default_auth_chart() -> RenderedChart:
    """:return: The chart rendered for Presto reading S3 archives with default authentication."""
    return render_chart(_default_auth_values())


@pytest.fixture(scope="module")
def static_auth_chart() -> RenderedChart:
    """:return: The chart rendered for Presto reading S3 archives with static credentials."""
    return render_chart(_static_auth_values())


@pytest.mark.parametrize(
    ("deployment_name", "container_name", "install_path_env"),
    [
        (COORDINATOR, "coordinator", "COORDINATOR_PLUGIN_INSTALL_PATH"),
        (WORKER, "worker", "WORKER_PLUGIN_INSTALL_PATH"),
    ],
)
def test_presto_pods_install_connector(
    presto_chart: RenderedChart,
    deployment_name: str,
    container_name: str,
    install_path_env: str,
) -> None:
    """
    Tests that each Presto pod copies the connector from its installer image into a volume that
    the Presto container loads as its CLP plugin.
    """
    installer = presto_chart.container(deployment_name, "install-clp-plugin")
    assert installer["image"].startswith(f"{CONNECTOR_REPOSITORY}:")
    assert "@sha256:" in installer["image"]
    (install_env,) = installer["env"]
    assert install_path_env == install_env["name"]
    assert [{"name": "presto-plugin", "mountPath": install_env["value"]}] == installer[
        "volumeMounts"
    ]

    presto = presto_chart.container(deployment_name, container_name)
    assert "@sha256:" in presto["image"]
    assert {"name": "presto-plugin", "mountPath": PLUGIN_DIR, "readOnly": True} in presto[
        "volumeMounts"
    ]
    assert {"name": "presto-plugin", "emptyDir": {}} in presto_chart.pod_spec(deployment_name)[
        "volumes"
    ]


def test_presto_loads_connector(presto_chart: RenderedChart) -> None:
    """
    Tests that the worker loads the connector from the plugin directory, and that the coordinator
    uses the connector's serialization codecs, which the worker needs to read the connector's
    handles.
    """
    coordinator_config = _config_map_properties(
        presto_chart, "coordinator-config-config.properties"
    )
    assert "true" == coordinator_config["use-connector-provided-serialization-codecs"]
    worker_config = _config_map_properties(presto_chart, "worker-config-config.properties")
    assert PLUGIN_DIR == worker_config["plugin.dir"]


def test_presto_reads_s3_archives_with_default_auth(default_auth_chart: RenderedChart) -> None:
    """
    Tests that, with default S3 authentication, the worker gets credentials from the AWS default
    credential provider chain through the release's service account, without static credentials.
    """
    assert {
        "connector.name": "clp",
        "clp.storage-type": "s3",
        "clp.s3-auth-provider": "default",
        "clp.s3-bucket": "example-bucket",
        "clp.s3-end-point": "https://s3.ca-central-1.amazonaws.com",
    } == _catalog(default_auth_chart, WORKER_CATALOG)

    service_account = f"{RELEASE_NAME}-service-account"
    assert service_account == default_auth_chart.pod_spec(WORKER)["serviceAccountName"]
    assert (
        "eks.amazonaws.com/role-arn"
        in (default_auth_chart.get("ServiceAccount", service_account)["metadata"]["annotations"])
    )
    worker_env = default_auth_chart.container(WORKER, "worker")["env"]
    assert {"name": "AWS_REGION", "value": "ca-central-1"} in worker_env
    assert {"name": "AWS_DEFAULT_REGION", "value": "ca-central-1"} in worker_env


def test_presto_reads_s3_archives_with_static_credentials(
    static_auth_chart: RenderedChart,
) -> None:
    """Tests that the worker catalog keeps reading S3 archives with static credentials."""
    assert {
        "connector.name": "clp",
        "clp.storage-type": "s3",
        "clp.s3-auth-provider": "clp_package",
        "clp.s3-access-key-id": STATIC_CREDENTIALS["access_key_id"],
        "clp.s3-secret-access-key": STATIC_CREDENTIALS["secret_access_key"],
        "clp.s3-bucket": "example-bucket",
        "clp.s3-end-point": "https://s3.ca-central-1.amazonaws.com",
    } == _catalog(static_auth_chart, WORKER_CATALOG)


@pytest.mark.parametrize("auth_type", ["env_vars", "profile"])
def test_presto_rejects_unsupported_s3_auth(auth_type: str) -> None:
    """Tests that S3 authentication types that Presto workers can't use fail to render."""
    aws_authentication: dict[str, Any] = {"type": auth_type}
    if "profile" == auth_type:
        aws_authentication["profile"] = "default"
    error = (
        f'Presto reads S3 archives with the "default" or "credentials" aws_authentication type,'
        f' not "{auth_type}".'
    )
    with pytest.raises(HelmTemplateError, match=re.escape(error)):
        render_chart(_s3_values(aws_authentication))


def test_presto_rejects_mismatched_archive_storage() -> None:
    """Tests that the subchart's archive storage, which Helm can't derive, must match CLP's."""
    values = _presto_values()
    values["clpConfig"]["archive_output"] = {"storage": {"type": "s3"}}
    error = (
        'clp-presto.archiveStorage.type is "fs" but clpConfig.archive_output.storage.type is "s3";'
        " they must match."
    )
    with pytest.raises(HelmTemplateError, match=re.escape(error)):
        render_chart(values)


@pytest.mark.parametrize("chart_fixture", ["default_auth_chart", "static_auth_chart"])
def test_presto_catalogs_are_secret(chart_fixture: str, request: pytest.FixtureRequest) -> None:
    """Tests that the catalogs, which may contain credentials, are only in the catalog Secret."""
    chart: RenderedChart = request.getfixturevalue(chart_fixture)
    assert "pass" == _catalog(chart, COORDINATOR_CATALOG)["clp.metadata-db-password"]

    for config_map_name in [CONFIG_MAP, PRESTO_CONFIG_MAP]:
        config_map_data = chart.get("ConfigMap", config_map_name)["data"]
        assert {COORDINATOR_CATALOG, WORKER_CATALOG}.isdisjoint(config_map_data)

    presto_config_data = "\n".join(chart.get("ConfigMap", PRESTO_CONFIG_MAP)["data"].values())
    assert "clp.metadata-db-password" not in presto_config_data
    assert STATIC_CREDENTIALS["secret_access_key"] not in presto_config_data

    for deployment_name, catalog_key in [
        (COORDINATOR, COORDINATOR_CATALOG),
        (WORKER, WORKER_CATALOG),
    ]:
        volumes = chart.pod_spec(deployment_name)["volumes"]
        (catalog_volume,) = [v for v in volumes if "presto-catalog" == v["name"]]
        assert {
            "secretName": CATALOG_SECRET,
            "items": [{"key": catalog_key, "path": "clp.properties"}],
        } == catalog_volume["secret"]


def test_presto_pods_roll_on_upgrade(presto_chart: RenderedChart) -> None:
    """
    Tests that both Presto pods carry the per-upgrade rollout annotation. The catalog comes from a
    Secret that this chart renders, so the subchart cannot hash it into the pod template; it rolls
    the pods on every upgrade instead.
    """
    for deployment_name in [COORDINATOR, WORKER]:
        annotations = presto_chart.get("Deployment", deployment_name)["spec"]["template"][
            "metadata"
        ]["annotations"]
        assert "checksum/release-revision" in annotations
        assert "checksum/presto-catalog" not in annotations


def test_presto_enabled_sets_webui_host(presto_chart: RenderedChart) -> None:
    """Tests that the Web UI is pointed at the subchart's coordinator service."""
    config = presto_chart.get("ConfigMap", CONFIG_MAP)["data"]["clp-config.yaml"]
    assert 'host: "clp-presto-coordinator"' in config
    assert "port: 8889" in config


def test_presto_disabled_renders_no_presto_workloads() -> None:
    """Tests that the default values render no Presto workloads."""
    chart = render_chart({})
    for kind, name in [
        ("Deployment", COORDINATOR),
        ("Deployment", WORKER),
        ("Service", COORDINATOR),
        ("ConfigMap", PRESTO_CONFIG_MAP),
        ("Secret", CATALOG_SECRET),
    ]:
        assert chart.find(kind, name) is None, f"{kind} '{name}' was rendered."

    config = chart.get("ConfigMap", CONFIG_MAP)["data"]["clp-config.yaml"]
    assert "presto: null" in config


@pytest.mark.parametrize(
    ("image", "expected_ref"),
    [
        ({"tag": "0.300", "digest": None}, "registry.example/presto:0.300"),
        ({"tag": None, "digest": "sha256:abc"}, "registry.example/presto@sha256:abc"),
        ({"tag": "0.300", "digest": "sha256:abc"}, "registry.example/presto:0.300@sha256:abc"),
    ],
)
def test_image_refs(image: dict[str, Any], expected_ref: str) -> None:
    """Tests that images can be selected by tag, digest, or both."""
    values = _presto_values()
    values["clp-presto"]["image"] = {
        "coordinator": {"repository": "registry.example/presto", **image},
    }
    chart = render_chart(values)
    assert expected_ref == chart.container(COORDINATOR, "coordinator")["image"]


def test_image_ref_requires_tag_or_digest() -> None:
    """Tests that an image with neither a tag nor a digest fails to render."""
    values = _presto_values()
    values["clp-presto"]["image"] = {"connector": {"tag": None, "digest": None}}
    error = 'image.connector requires "tag" or "digest"'
    with pytest.raises(HelmTemplateError, match=re.escape(error)):
        render_chart(values)
