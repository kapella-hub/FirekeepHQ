"""The publish workflow and the compose file must describe the same images.

Until 2026-07-27 there was no way to deliver the server at all: the docs told a
customer to `git clone` a PRIVATE repo, and `install.sh` assumed it was running
inside a checkout and built every image from source. No tarball, no `vX.Y.Z`
tag, no registry.

Now `docker-compose.yml` names a published image for each built service and
`.github/workflows/server-release.yml` pushes them on a `v*` tag. Those two
files have to agree, and nothing else checks that they do — a service whose
`image:` names something the workflow never pushes fails at the customer's
`docker compose pull` with `manifest unknown`, which is the worst possible place
to discover it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "server-release.yml"

# Datastores the CUSTOMER pulls from their own upstream. Publishing an image
# containing one of these would make Firekeep a redistributor of it rather than
# a party that references it — for Neo4j that means GPLv3 obligations attach to
# us. See docs/THIRD-PARTY-DATASTORES.md.
NEVER_PUBLISH = ("neo4j", "redis", "qdrant", "ollama", "nginx", "caddy", "postgres")


def _compose() -> dict:
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _built_services() -> dict[str, str]:
    """{service: image ref} for every service compose BUILDS."""
    return {
        n: s.get("image", "")
        for n, s in (_compose().get("services") or {}).items()
        if s.get("build")
    }


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _published_names() -> set[str]:
    entries = _workflow()["jobs"]["publish"]["strategy"]["matrix"]["include"]
    return {e["name"] for e in entries}


def test_every_built_service_names_a_published_image() -> None:
    built = _built_services()
    assert built, "no built services found — this check would be vacuous"
    missing = sorted(n for n, img in built.items() if not img)
    assert not missing, (
        f"{missing} are built by compose but name no image, so there is nothing to "
        f"publish and nothing for a customer to pull"
    )


def test_built_images_all_point_at_the_registry() -> None:
    for svc, img in _built_services().items():
        assert img.startswith("ghcr.io/kapella-hub/firekeep-"), f"{svc}: {img}"


def test_the_four_cortex_services_share_one_image() -> None:
    """They differ only by command. Four images would be four times the pull."""
    cortex = {img for svc, img in _built_services().items() if svc.startswith("cortex-")}
    assert len(cortex) == 1, f"cortex services resolve to {len(cortex)} images: {cortex}"


def test_workflow_publishes_every_image_compose_references() -> None:
    """The check that catches `manifest unknown` at the customer's first pull."""
    referenced = {
        re.sub(r"^ghcr\.io/kapella-hub/firekeep-", "", img).split(":")[0]
        for img in _built_services().values()
        if img
    }
    published = _published_names()
    assert referenced, "parsed no image names — vacuous"
    missing = sorted(referenced - published)
    assert not missing, (
        f"compose references {missing} but server-release.yml never pushes them. "
        f"A customer's `docker compose pull` fails with 'manifest unknown'."
    )


def test_workflow_does_not_publish_a_third_party_datastore() -> None:
    """The conveyance boundary, asserted rather than trusted to a comment.

    Firekeep references neo4j/redis/qdrant/ollama; the customer's own daemon
    fetches them. Publishing an image that CONTAINS one makes us a redistributor
    — and for Neo4j (GPLv3) that attaches obligations to us instead of passing
    them through. docker/Dockerfile.{neo4j,redis,qdrant,ollama} exist and would
    do exactly that if wired into this workflow.
    """
    entries = _workflow()["jobs"]["publish"]["strategy"]["matrix"]["include"]
    for e in entries:
        for banned in NEVER_PUBLISH:
            assert banned not in e["name"], f"publishing '{e['name']}' conveys {banned}"
            assert banned not in e["dockerfile"].lower(), (
                f"{e['dockerfile']} builds a third-party datastore image"
            )


def test_workflow_builds_the_dockerfile_compose_builds() -> None:
    """A published image must come from the file the shipped compose names.

    There are TWO cortex Dockerfiles — cortex/Dockerfile (compose) and the
    repo-root one (CI). Publishing from the other would hand a customer an image
    built from a different file than the compose they run describes, which is the
    reproducibility hole the dependency locking closed.
    """
    services = _compose()["services"]
    by_image: dict[str, set[str]] = {}
    for svc, spec in services.items():
        if not spec.get("build"):
            continue
        name = re.sub(r"^ghcr\.io/kapella-hub/firekeep-", "", spec["image"]).split(":")[0]
        by_image.setdefault(name, set()).add(spec["build"].get("dockerfile", ""))

    for e in _workflow()["jobs"]["publish"]["strategy"]["matrix"]["include"]:
        compose_dockerfiles = by_image.get(e["name"], set())
        assert compose_dockerfiles, f"workflow publishes '{e['name']}' which compose never builds"
        assert e["dockerfile"] in compose_dockerfiles, (
            f"workflow builds {e['name']} from {e['dockerfile']} but compose builds it "
            f"from {compose_dockerfiles}"
        )


def test_release_is_tag_triggered_not_push_triggered() -> None:
    """A release must be a deliberate act, not a consequence of merging."""
    on = _workflow()[True] if True in _workflow() else _workflow()["on"]
    assert "push" in on and "tags" in on["push"], "server-release must trigger on tags"
    assert "branches" not in on.get("push", {}), (
        "server-release triggers on branch pushes — every merge would publish"
    )


def test_release_cannot_publish_an_arbitrary_ref_by_manual_dispatch() -> None:
    """A version tag must identify the commit whose images it names."""
    on = _workflow()[True] if True in _workflow() else _workflow()["on"]
    assert "workflow_dispatch" not in on, (
        "manual dispatch can publish a version name from an unrelated checkout ref"
    )


def test_every_published_image_contains_licence_and_notice() -> None:
    """Public BUSL images must carry Firekeep's licence and dependency notices."""
    entries = _workflow()["jobs"]["publish"]["strategy"]["matrix"]["include"]
    for entry in entries:
        dockerfile = REPO / entry["dockerfile"]
        text = dockerfile.read_text(encoding="utf-8")
        assert "COPY LICENSE NOTICE ./" in text, (
            f"{entry['dockerfile']} omits LICENSE or NOTICE from the published image"
        )
        assert (
            'LABEL org.opencontainers.image.source="https://github.com/kapella-hub/FirekeepHQ"'
            in text
        ), f"{entry['dockerfile']} will not link its GHCR package to this repository"

    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "Path('/app/LICENSE').stat().st_size" in workflow
    assert "Path('/app/NOTICE').stat().st_size" in workflow


def test_release_tags_are_not_overwritten_on_rerun() -> None:
    """Visibility retries must verify the first artifact, not replace it."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'docker manifest inspect "$REF"' in workflow
    assert "LOOKUP_STATUS=$?" in workflow
    assert "manifest unknown|name unknown|no such manifest" in workflow
    assert "registry lookup failed; refusing to overwrite" in workflow
    assert "FIRST_SERVER_RELEASE: v0.1.0" in workflow
    assert "api.github.com/user/packages/container/firekeep-" in workflow
    assert "api.github.com/users/${OWNER}/packages" not in workflow
    assert 'API_STATUS" = "404"' in workflow
    assert 'steps.meta.outputs.tag }}" = "$FIRST_SERVER_RELEASE"' in workflow
    assert "package exists but registry lookup was denied; refusing to overwrite" in workflow
    assert 'docker pull "$REF"' in workflow
    assert 'docker push "$REF"' in workflow
    assert workflow.index('docker manifest inspect "$REF"') < workflow.index('docker push "$REF"')

    assert 'if [ -e "$VERSIONED" ]' in workflow
    assert 'Reusing immutable versioned bundle $VERSIONED' in workflow
    assert 'sha256sum -c -' in workflow
    assert 'cp "$VERSIONED/server.json" gh/server/latest/server.json' in workflow
    assert 'echo "expect_latest=$publish_latest" >> "$GITHUB_OUTPUT"' in workflow
    assert '[ "$EXPECT_LATEST" != "1" ] || [ "$got" = "$TAG" ]' in workflow
    assert "cmp public-server.json public-latest-server.json" in workflow


# --- the customer install path ------------------------------------------------

def _install_sh() -> str:
    return (REPO / "install.sh").read_text(encoding="utf-8")


def test_install_supports_a_pull_mode() -> None:
    """A customer must be able to install without the source.

    Before this, install.sh built all seven services from a git checkout — so
    the only way to run Firekeep was to be handed the code the licence does not
    grant rights to.
    """
    code = "\n".join(ln for ln in _install_sh().splitlines() if not ln.lstrip().startswith("#"))
    assert '"--pull"' in code, "install.sh has no --pull mode"
    assert "docker compose pull" in code, "--pull never actually pulls"
    assert "docker compose up -d --build" in code, "the build path was lost"


def test_pull_mode_refuses_the_unpublished_default() -> None:
    """`dev` is never published. Failing early beats 'manifest unknown' later.

    The check must happen BEFORE .env is written and Redis is started, or the
    operator debugs a half-built install instead of reading one line.
    """
    code = _install_sh()
    assert 'IMAGE_TAG_VALUE" = "dev"' in code, (
        "install.sh --pull does not reject the unpublished default tag"
    )
    assert "manifest unknown" in code, "the failure message does not name the symptom"


def test_env_example_and_compose_agree_on_the_default_tag() -> None:
    """Two places name the default; they must not drift.

    compose says `${IMAGE_TAG:-dev}` and .env.example ships `IMAGE_TAG=dev`. If
    those disagreed, a developer's build and a `.env`-driven run would tag
    different images and neither would be obviously wrong.
    """
    env_default = None
    for raw in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("IMAGE_TAG="):
            env_default = raw.split("=", 1)[1].strip()
            break
    assert env_default, ".env.example does not ship an IMAGE_TAG"

    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    fallbacks = set(re.findall(r"\$\{IMAGE_TAG:-([^}]+)\}", compose))
    assert fallbacks, "compose does not reference ${IMAGE_TAG:-...}"
    assert fallbacks == {env_default}, (
        f".env.example ships IMAGE_TAG={env_default} but compose falls back to "
        f"{fallbacks} — a build and a .env-driven run would tag different images"
    )


def test_docs_tell_a_customer_how_to_install_without_source() -> None:
    """README and DEPLOYMENT both told customers to `git clone` a PRIVATE repo."""
    for name in ("README.md", "docs/DEPLOYMENT.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "install.sh --pull" in text, f"{name} does not document the customer path"
        assert "docker login ghcr.io" not in text, f"{name} still requires registry credentials"


def test_release_verifies_anonymous_image_pull() -> None:
    """Download access cannot become a hidden second entitlement system."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    logout = workflow.index("docker logout ghcr.io")
    pull = workflow.index('docker pull "${{ steps.meta.outputs.image }}', logout)
    assert logout < pull
    assert "public package" in _install_sh().lower() or "public visibility" in _install_sh().lower()
