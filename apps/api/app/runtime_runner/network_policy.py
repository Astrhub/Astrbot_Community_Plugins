from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from .docker_cli import DockerCommandClient, DockerCommandResult
from .execution import RuntimeExecutionError

NETWORK_POLICY_VERSION = "install-proxy-v1"


@dataclass(frozen=True, slots=True)
class NetworkPolicySnapshot:
    backend: str
    profile: str
    install_egress_enforced: bool
    private_network_blocked: bool
    metadata_endpoint_blocked: bool
    host_gateway_blocked: bool
    site_services_blocked: bool
    smoke_network_disabled: bool = True


class DockerNetworkPolicy:
    def __init__(
        self,
        client: DockerCommandClient,
        *,
        network_name: str,
        package_index_url: str,
        proxy_url: str,
        proxy_container: str,
    ) -> None:
        self.client = client
        self.network_name = network_name
        self.package_index_url = package_index_url.rstrip("/")
        self.proxy_url = proxy_url.rstrip("/")
        self.proxy_container = proxy_container

    async def verify(self, profile: str) -> NetworkPolicySnapshot:
        inspected = await self.client.execute(
            ("network", "inspect", self.network_name, "--format", "{{json .}}"),
            timeout_seconds=20,
            max_output_bytes=2 * 1024 * 1024,
        )
        _require_success(inspected)
        network = _json_object(inspected.stdout)
        expected_labels = required_network_labels(profile, self.package_index_url)
        labels = network.get("Labels")
        options = network.get("Options")
        if (
            network.get("Name") != self.network_name
            or network.get("Driver") != "bridge"
            or network.get("Scope") not in {None, "local"}
            or network.get("Internal") is not True
            or not isinstance(labels, dict)
            or any(str(labels.get(key) or "") != value for key, value in expected_labels.items())
            or not isinstance(options, dict)
            or str(options.get("com.docker.network.bridge.enable_ip_masquerade")).casefold()
            != "false"
        ):
            raise _unverified()

        peers = network.get("Containers") or {}
        if not isinstance(peers, dict):
            raise _unverified()
        proxy_seen = False
        for peer in peers.values():
            if not isinstance(peer, dict):
                raise _unverified()
            name = str(peer.get("Name") or "")
            peer_labels = await self._container_labels(name)
            if name == self.proxy_container:
                proxy_seen = True
                if (
                    peer_labels.get("astrbot.runtime.package-proxy") != "true"
                    or peer_labels.get("astrbot.runtime.package-index-sha256")
                    != package_index_sha256(self.package_index_url)
                ):
                    raise _unverified()
            elif peer_labels.get("astrbot.runtime.managed") != "true":
                raise _unverified()
        if not proxy_seen or urlsplit(self.proxy_url).hostname != self.proxy_container:
            raise _unverified()
        return NetworkPolicySnapshot(
            backend="docker-internal-proxy-v1",
            profile=profile,
            install_egress_enforced=True,
            private_network_blocked=True,
            metadata_endpoint_blocked=True,
            host_gateway_blocked=True,
            site_services_blocked=True,
        )

    async def _container_labels(self, name: str) -> dict[str, str]:
        if not name:
            raise _unverified()
        inspected = await self.client.execute(
            ("container", "inspect", name, "--format", "{{json .Config.Labels}}"),
            timeout_seconds=15,
            max_output_bytes=256 * 1024,
        )
        _require_success(inspected)
        labels = _json_object(inspected.stdout)
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
            raise _unverified()
        return {str(key): str(value) for key, value in labels.items()}


def required_network_labels(profile: str, package_index_url: str) -> dict[str, str]:
    return {
        "astrbot.runtime.policy": NETWORK_POLICY_VERSION,
        "astrbot.runtime.profile": profile,
        "astrbot.runtime.package-index-sha256": package_index_sha256(package_index_url),
        "astrbot.runtime.package-proxy-only": "true",
        "astrbot.runtime.private-network-blocked": "true",
        "astrbot.runtime.metadata-endpoint-blocked": "true",
        "astrbot.runtime.host-gateway-blocked": "true",
        "astrbot.runtime.site-services-blocked": "true",
    }


def package_index_sha256(value: str) -> str:
    return hashlib.sha256(value.rstrip("/").encode()).hexdigest()


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _unverified() from exc
    if not isinstance(parsed, dict):
        raise _unverified()
    return parsed


def _require_success(result: DockerCommandResult) -> None:
    if not result.succeeded:
        raise _unverified()


def _unverified() -> RuntimeExecutionError:
    return RuntimeExecutionError(
        "runtime_network_unverified",
        "Runtime install network policy could not be verified",
    )
