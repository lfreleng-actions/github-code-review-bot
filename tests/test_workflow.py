# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline contracts over the workflow YAML: trust boundary, pins, plumbing.

The reusable workflow separates a trusted ``select`` job, an untrusted
``review`` matrix and a trusted ``apply`` matrix (docs/DESIGN.md
sections 4 and 5). These tests read the parsed documents, never their
comments, and pin the properties a reviewer would otherwise re-derive
by hand on every change: which job holds which credential, that
downloads go by producer artifact ID, that every action is pinned to
a commit, and that the one write is minted for one repository on the
live path alone.

Two modes share the pipeline. The approve mode's caller holds the
approving App and the antagonistic mode's caller a second App; each
mode checks minted tokens against its own configured slug, and the
antagonistic path carries no approval event at all.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml  # pyright: ignore[reportMissingModuleSource]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / "scripts"
REUSABLE = WORKFLOWS / "code-review.yaml"
CRON = WORKFLOWS / "code-review-cron.yaml"
ANTAGONISTIC = WORKFLOWS / "code-review-antagonistic.yaml"
TESTING = WORKFLOWS / "testing.yaml"

# PyYAML reads the bare mapping key ``on`` as the boolean ``True``.
TRIGGERS = True

HARDEN_RUNNER = "step-security/harden-runner"
BLOCK_ACTION = "lfreleng-actions/harden-runner-block-action"
CHECKOUT = "actions/checkout"
APP_TOKEN = "actions/create-github-app-token"
SPLITTER = "lfreleng-actions/list-splitter-action"
DOWNLOAD = "actions/download-artifact"
UPLOAD = "actions/upload-artifact"
REUSABLE_CALL = "$/.github/workflows/code-review.yaml"
TRUSTED_JOBS = ("select", "apply", "report", "handback")
# Review events GitHub accepts besides COMMENT; the antagonistic path
# must never be able to send either.
FORBIDDEN_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})
# The modules the antagonistic apply path loads; apply_review, which
# approves, must not be among them.
COMMENT_PATH = (
    "comment_review",
    "comment_body",
    "comment_markdown",
    "review_comments",
    "review_files",
)

COMMIT_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SECRET_REFERENCE = re.compile(r"secrets\.[A-Za-z0-9_]+")
ALLOW_CONFIG = re.compile(r"^@[0-9a-f]{40}$")


def squash(text: str) -> str:
    """Collapse runs of whitespace so block scalars compare line-wrap free."""
    return re.sub(r"\s+", " ", text).strip()


def flatten(script: str) -> str:
    """Join backslash continuations, then squash, for shell substring checks."""
    return squash(re.sub(r"\\\n\s*", " ", script))


def uses_of(step: dict[str, Any]) -> str:
    """Return the action coordinate of a step, or an empty string."""
    return str(step.get("uses", ""))


def is_action(step: dict[str, Any], action: str) -> bool:
    """Match an action invocation independently of its commit pin."""
    return uses_of(step).startswith(action + "@")


def dumped(node: object) -> str:
    """Serialise a YAML subtree so a regex can scan every value in it."""
    return json.dumps(node, sort_keys=True)


def module_tree(name: str) -> ast.Module:
    """Parse one script module from the assets checkout."""
    return ast.parse((SCRIPTS / f"{name}.py").read_text(encoding="utf-8"))


def string_constants(tree: ast.Module) -> set[str]:
    """Every string literal a module holds, docstrings included."""
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def imported_modules(tree: ast.Module) -> set[str]:
    """The top-level names of every module a module imports."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class WorkflowCase(unittest.TestCase):
    """Read workflow structure, never comments."""

    WORKFLOW: ClassVar[Path]

    def setUp(self) -> None:
        """Load a fresh document for each contract."""
        loaded = yaml.safe_load(self.WORKFLOW.read_text(encoding="utf-8"))
        self.workflow: dict[str | bool, Any] = cast(dict[str | bool, Any], loaded)
        self.jobs: dict[str, dict[str, Any]] = cast(
            dict[str, dict[str, Any]], self.workflow["jobs"]
        )
        self.triggers: dict[str, Any] = cast(dict[str, Any], self.workflow[TRIGGERS])

    def steps(self, job: str) -> list[dict[str, Any]]:
        """Return the step list of a job that runs on a runner."""
        return cast(list[dict[str, Any]], self.jobs[job]["steps"])

    def step(self, job: str, identity: str) -> dict[str, Any]:
        """Find exactly one step by id or name."""
        matches = [
            s for s in self.steps(job) if identity in (s.get("id"), s.get("name"))
        ]
        self.assertEqual(len(matches), 1, f"{job}: expected one {identity!r} step")
        return matches[0]

    def position(self, job: str, identity: str) -> int:
        """Return the index of a step so ordering contracts can compare."""
        return self.steps(job).index(self.step(job, identity))

    def actions(self, job: str, action: str) -> list[dict[str, Any]]:
        """Select action invocations independently of their commit pin."""
        return [s for s in self.steps(job) if is_action(s, action)]

    def action(self, job: str, action: str) -> dict[str, Any]:
        """Return the single invocation of an action within a job."""
        matches = self.actions(job, action)
        self.assertEqual(len(matches), 1, f"{job}: expected one {action} step")
        return matches[0]

    def assert_pinned_steps(self) -> None:
        """Every step-level `uses:` names a 40-hex commit, never a tag or branch."""
        for job in self.jobs:
            if "steps" not in self.jobs[job]:
                continue
            for step in self.steps(job):
                if "uses" not in step:
                    continue
                with self.subTest(job=job, uses=step["uses"]):
                    self.assertRegex(uses_of(step), COMMIT_PIN)


class ReusableWorkflowCase(WorkflowCase):
    """Shared fixture for the reusable workflow contracts."""

    WORKFLOW = REUSABLE


class TopLevelContracts(ReusableWorkflowCase):
    """Permissions, concurrency, pins and runner hardening across all jobs."""

    def test_default_permissions_are_empty(self) -> None:
        """Every job opts into its own grants; the workflow default is none."""
        self.assertEqual(self.workflow["permissions"], {})

    def test_concurrency_separates_dry_runs_from_live_runs(self) -> None:
        """Live approve runs lock per caller and owner; dry and antagonistic per run."""
        group = squash(str(self.workflow["concurrency"]["group"]))
        self.assertIn("inputs.dry_run", group)
        self.assertIn("code-review-dry-run-{0}-{1}', github.run_id", group)
        self.assertIn("inputs.mode == 'antagonistic'", group)
        self.assertIn("code-review-antagonistic-{0}-{1}', github.run_id", group)
        self.assertIn("code-review-{0}-{1}', github.repository", group)
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])

    def test_every_action_is_pinned_to_a_commit(self) -> None:
        """No step uses a floating tag or branch."""
        self.assert_pinned_steps()

    def test_every_checkout_drops_credentials(self) -> None:
        """No checkout persists a token into its working tree."""
        for job in self.jobs:
            for step in self.actions(job, CHECKOUT):
                with self.subTest(job=job, path=step["with"].get("path")):
                    self.assertIs(step["with"]["persist-credentials"], False)

    def test_harden_runner_leads_every_job(self) -> None:
        """Egress hardening is the first action in each job; trusted jobs honour the input."""
        for job in self.jobs:
            steps = self.steps(job)
            first_action = next(s for s in steps if "uses" in s)
            with self.subTest(job=job):
                if job in TRUSTED_JOBS:
                    self.assertTrue(is_action(first_action, BLOCK_ACTION))
                    harden = self.action(job, HARDEN_RUNNER)
                    self.assertEqual(
                        harden["with"]["egress-policy"], "${{ inputs.egress_policy }}"
                    )
                else:
                    self.assertTrue(is_action(first_action, HARDEN_RUNNER))
                    self.assertEqual(first_action["with"]["egress-policy"], "audit")

    def test_review_job_never_loads_the_allow_list(self) -> None:
        """The review job audits: the model backend is outside the allow-list."""
        self.assertEqual(self.actions("review", BLOCK_ACTION), [])


class SelectContracts(ReusableWorkflowCase):
    """The trusted select job: scoped reads, ledger, evidence and matrix."""

    def test_read_token_is_read_only(self) -> None:
        """The scan token reads alone; ListInputContracts pins its scope."""
        mint = self.action("select", APP_TOKEN)
        grants = {k: v for k, v in mint["with"].items() if k.startswith("permission-")}
        self.assertEqual(set(grants.values()), {"read"})

    def test_live_runs_require_the_app(self) -> None:
        """Without a client id the workflow refuses to leave dry-run."""
        guard = self.step("select", "Require App for live runs")
        self.assertIn("!inputs.dry_run", guard["if"])
        self.assertIn("inputs.github_app_client_id == ''", guard["if"])

    def test_ledger_comes_from_the_native_token_before_selection(self) -> None:
        """The prior ledger is this repository's artifact, read with GITHUB_TOKEN."""
        ledger = self.step("select", "Fetch prior ledger")
        self.assertEqual(ledger["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn("ledger.py fetch", flatten(ledger["run"]))
        self.assertIn('--artifact-name "$LEDGER_NAME"', flatten(ledger["run"]))
        self.assertEqual(
            ledger["env"]["LEDGER_NAME"], "${{ steps.budget.outputs.ledger_name }}"
        )
        self.assertLess(
            self.position("select", "Fetch prior ledger"),
            self.position("select", "Select pull requests"),
        )
        select = self.step("select", "Select pull requests")
        self.assertIn("--ledger artefacts/ledger.json", flatten(select["run"]))
        self.assertEqual(
            select["env"]["GH_TOKEN"],
            "${{ steps.app-token.outputs.token || github.token }}",
        )
        self.assertEqual(self.jobs["select"]["permissions"]["actions"], "read")

    def test_each_mode_names_its_app_and_its_ledger(self) -> None:
        """The mode, checked before any token, picks the App config and ledger.

        An unknown mode fails, and the antagonistic mode refuses any
        approve tier, so no caller can configure it to approve.
        """
        step = self.step("select", "Check inputs")
        self.assertEqual(step["env"]["MODE"], "${{ inputs.mode }}")
        run = flatten(step["run"])
        self.assertIn(
            "approve) bot_config='config/bot.json' ledger_name='code-review-ledger' ;;",
            run,
        )
        self.assertIn(
            "bot_config='config/antagonistic-bot.json'"
            " ledger_name='code-review-antagonistic-ledger' ;;",
            run,
        )
        self.assertIn("echo '::error::the antagonistic mode approves nothing'", run)
        self.assertIn("*) echo '::error::mode must be approve or antagonistic'", run)
        self.assertLess(
            self.position("select", "Check inputs"),
            self.position("select", "Mint read-only App token"),
        )
        outputs = self.jobs["select"]["outputs"]
        self.assertEqual(
            outputs["bot_config"], "${{ steps.budget.outputs.bot_config }}"
        )
        self.assertEqual(
            outputs["ledger_name"], "${{ steps.budget.outputs.ledger_name }}"
        )

    def test_selection_forwards_every_scoping_input(self) -> None:
        """Mode, named pull requests, repositories, tiers and the cap reach the script."""
        run = flatten(self.step("select", "Select pull requests")["run"])
        for flag in (
            '--mode "$MODE"',
            "--bot-authors-file review-assets/config/antagonistic-authors.txt",
            '--pull-requests "$PULL_REQUESTS"',
            '--repositories "$REPOSITORIES"',
            '--approve-tiers "$APPROVE_TIERS"',
            '--max-pull-requests "$MAX_PRS"',
            "--exclude-file review-assets/config/excluded-repos.txt",
            '--bot-slug "$BOT_SLUG"',
        ):
            self.assertIn(flag, run)

    def test_skip_agent_and_empty_selection_stop_the_sessions(self) -> None:
        """run_agent is false for plumbing runs and for runs that select nothing."""
        run = flatten(self.step("select", "Publish selection outputs")["run"])
        self.assertIn('[ "$SKIP_AGENT" != true ] && [ "$count" != 0 ]', run)
        self.assertEqual(
            self.jobs["review"]["if"], "needs.select.outputs.run_agent == 'true'"
        )

    def test_select_publishes_the_full_provenance_set(self) -> None:
        """Downstream jobs receive the asset SHA, evidence ID and both digests."""
        outputs = self.jobs["select"]["outputs"]
        self.assertEqual(outputs["assets_sha"], "${{ steps.pinned.outputs.sha }}")
        self.assertEqual(
            outputs["evidence_id"], "${{ steps.evidence.outputs.artifact-id }}"
        )
        self.assertEqual(
            outputs["selection_sha256"], "${{ steps.digests.outputs.selection }}"
        )
        self.assertEqual(
            outputs["ledger_sha256"], "${{ steps.digests.outputs.ledger }}"
        )
        evidence = self.action("select", UPLOAD)
        self.assertEqual(evidence["with"]["retention-days"], 7)
        self.assertEqual(evidence["with"]["if-no-files-found"], "error")


class PreflightContracts(ReusableWorkflowCase):
    """The run-time gate runs from pinned assets before any token exists."""

    def test_gate_runs_after_pinning_and_before_the_read_mint(self) -> None:
        """Contract tests, zizmor and the config check precede the mint."""
        pinned = self.position("select", "Pin assets commit")
        workflow = self.position("select", "Pre-flight: workflow contracts and audit")
        config = self.position("select", "Pre-flight: configuration and credentials")
        mint = self.position("select", "Mint read-only App token")
        self.assertLess(pinned, workflow)
        self.assertLess(workflow, config)
        self.assertLess(config, mint)
        run = flatten(
            self.step("select", "Pre-flight: workflow contracts and audit")["run"]
        )
        self.assertIn("scripts/preflight.py workflow --root .", run)
        self.assertIn("cd review-assets", run)

    def test_config_gate_sees_the_credentials_under_template_names(self) -> None:
        """Shape checks read the inputs the workflow was handed, nothing else."""
        step = self.step("select", "Pre-flight: configuration and credentials")
        self.assertEqual(
            step["env"]["BOT_APP_CLIENT_ID"], "${{ inputs.github_app_client_id }}"
        )
        self.assertEqual(
            step["env"]["BOT_APP_PRIVATE_KEY"], "${{ secrets.github_app_private_key }}"
        )
        self.assertEqual(
            step["env"]["COPILOT_GITHUB_TOKEN"], "${{ secrets.copilot_token }}"
        )
        run = flatten(step["run"])
        self.assertIn('--config "review-assets/$BOT_CONFIG"', run)
        self.assertEqual(
            step["env"]["BOT_CONFIG"], "${{ steps.budget.outputs.bot_config }}"
        )
        self.assertIn(
            '--workflow-sha "$JOB_WORKFLOW_SHA" --assets-sha "$ASSETS_SHA"', run
        )

    def test_minted_tokens_are_checked_for_identity(self) -> None:
        """Both mints hand their app-slug to the mode's identity check before use."""
        read = self.step("select", "Pre-flight: App identity and token grants")
        self.assertEqual(
            read["env"]["MINTED_SLUG"], "${{ steps.app-token.outputs.app-slug }}"
        )
        self.assertEqual(
            read["env"]["GH_TOKEN"], "${{ steps.app-token.outputs.token }}"
        )
        self.assertIn("preflight.py token", flatten(read["run"]))
        self.assertEqual(
            read["env"]["BOT_CONFIG"], "${{ steps.budget.outputs.bot_config }}"
        )
        self.assertIn('--config "review-assets/$BOT_CONFIG"', flatten(read["run"]))
        self.assertLess(
            self.position("select", "Pre-flight: App identity and token grants"),
            self.position("select", "Fetch prior ledger"),
        )
        write = self.step("apply", "Pre-flight: write token identity")
        self.assertEqual(
            write["env"]["MINTED_SLUG"], "${{ steps.write-token.outputs.app-slug }}"
        )
        self.assertEqual(
            write["env"]["BOT_CONFIG"], "${{ needs.select.outputs.bot_config }}"
        )
        self.assertIn('--config "review-assets/$BOT_CONFIG"', flatten(write["run"]))
        self.assertLess(
            self.position("apply", "Pre-flight: write token identity"),
            self.position("apply", "Apply verdict"),
        )

    def test_zizmor_is_pinned(self) -> None:
        """The auditor the gate runs is an exact version."""
        run = flatten(self.step("select", "Install zizmor")["run"])
        self.assertRegex(run, r"uv tool install 'zizmor==\d+\.\d+\.\d+'")


class MintProvenanceContracts(ReusableWorkflowCase):
    """No token is ever minted for an owner or scope from untrusted input."""

    def test_every_mint_owner_is_the_trusted_org_input(self) -> None:
        """owner is literally inputs.org on every create-github-app-token step."""
        mints = [
            (job, step)
            for job in self.jobs
            if "steps" in self.jobs[job]
            for step in self.actions(job, APP_TOKEN)
        ]
        self.assertEqual({job for job, _ in mints}, {"select", "apply"})
        for job, step in mints:
            with self.subTest(job=job):
                self.assertEqual(step["with"]["owner"], "${{ inputs.org }}")
                self.assertEqual(
                    step["with"]["client-id"], "${{ inputs.github_app_client_id }}"
                )
                self.assertEqual(
                    step["with"]["private-key"], "${{ secrets.github_app_private_key }}"
                )

    def test_write_mint_scope_is_one_recorded_repository(self) -> None:
        """The only write token names a single matrix repository, never a list."""
        mint = self.action("apply", APP_TOKEN)
        self.assertEqual(mint["with"]["repositories"], "${{ matrix.repo_name }}")


class ReviewContracts(ReusableWorkflowCase):
    """The untrusted review job: no App key, bounded tools, typed outputs."""

    def test_review_holds_no_app_key(self) -> None:
        """No action in the review job receives the private key, and no mint runs."""
        self.assertEqual(self.actions("review", APP_TOKEN), [])
        self.assertNotIn("github_app_private_key", dumped(self.jobs["review"]))
        self.assertEqual(self.jobs["review"]["permissions"], {"contents": "read"})
        secrets = set(SECRET_REFERENCE.findall(dumped(self.jobs["review"])))
        self.assertEqual(secrets, {"secrets.copilot_token"})

    def test_budget_and_fan_out_come_from_select(self) -> None:
        """Timeouts and parallelism are computed once in the trusted job."""
        job = self.jobs["review"]
        self.assertEqual(
            job["timeout-minutes"],
            "${{ fromJSON(needs.select.outputs.review_timeout) }}",
        )
        self.assertEqual(
            job["strategy"]["max-parallel"],
            "${{ fromJSON(needs.select.outputs.max_parallel) }}",
        )
        self.assertEqual(
            job["strategy"]["matrix"], "${{ fromJSON(needs.select.outputs.matrix) }}"
        )
        self.assertFalse(job["strategy"]["fail-fast"])

    def test_evidence_arrives_by_id_and_is_verified_before_use(self) -> None:
        """The packet is fetched by producer ID and checked against trusted digests."""
        download = self.action("review", DOWNLOAD)
        self.assertEqual(
            download["with"]["artifact-ids"], "${{ needs.select.outputs.evidence_id }}"
        )
        for job in ("review", "apply", "report"):
            run = flatten(self.step(job, "Verify evidence bytes")["run"])
            self.assertIn("bot_evidence.py verify", run)
            self.assertIn('--expect "selection.json=$SELECTION_SHA"', run)
            self.assertIn('--expect "ledger.json=$LEDGER_SHA"', run)
        self.assertLess(
            self.position("review", "Verify evidence bytes"),
            self.position("review", "Prepare packet and prompt"),
        )

    def test_head_checkout_is_the_recorded_commit(self) -> None:
        """The context checkout is the head SHA the selection recorded, no credential."""
        checkouts = self.actions("review", CHECKOUT)
        head = next(s for s in checkouts if s["with"].get("path") == "workspace")
        self.assertEqual(head["with"]["repository"], "${{ matrix.head_repository }}")
        self.assertEqual(head["with"]["ref"], "${{ matrix.head_sha }}")
        packet = flatten(self.step("review", "Prepare packet and prompt")["run"])
        self.assertIn('[ "$(git -C workspace rev-parse HEAD)" = "$HEAD_SHA" ]', packet)

    def test_each_mode_reads_its_own_task(self) -> None:
        """The prompt comes from the trusted assets, chosen by the trusted mode."""
        step = self.step("review", "Prepare packet and prompt")
        self.assertEqual(step["env"]["MODE"], "${{ inputs.mode }}")
        packet = flatten(step["run"])
        self.assertIn("approve) task='review-assets/prompt/review.md'", packet)
        self.assertIn(
            "antagonistic) task='review-assets/prompt/antagonistic.md'", packet
        )
        self.assertIn("*) echo '::error::unknown mode'; exit 1 ;;", packet)
        self.assertIn('cat "$task"', packet)

    def test_agent_invocation_is_confined(self) -> None:
        """Read-only tools, denied gh/git/write, redacted PAT, typed outputs."""
        run = flatten(self.step("review", "Run review agent (Copilot)")["run"])
        self.assertIn("github_pat_*) ;;", run)
        self.assertIn("for cmd in cat jq grep head tail wc ls; do", run)
        self.assertIn(
            "--deny-tool='write,shell(gh),shell(gh:*),shell(git),shell(git:*)'", run
        )
        self.assertIn("--secret-env-vars=COPILOT_GITHUB_TOKEN", run)
        self.assertIn(
            "--no-ask-user --no-custom-instructions --disable-builtin-mcps", run
        )
        self.assertIn("--usage-output-file artefacts/usage.json", run)
        self.assertIn("--share=artefacts/session-summary.md", run)
        self.assertNotIn("--allow-all-tools", run)

    def test_cli_installs_from_the_committed_lockfile(self) -> None:
        """The Copilot CLI comes from npm ci against the verified assets checkout."""
        run = flatten(self.step("review", "Install Copilot CLI")["run"])
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", run)
        self.assertNotIn("npm install -g", run)

    def test_session_artifact_is_stable_per_key(self) -> None:
        """One session name per pull request so the apply job always finds it."""
        upload = self.action("review", UPLOAD)
        self.assertEqual(
            upload["with"]["name"],
            "code-review-session-${{ needs.select.outputs.namespace }}-${{ matrix.key }}",
        )
        self.assertTrue(upload["with"]["overwrite"])
        self.assertEqual(upload["with"]["retention-days"], 7)

    def test_cleanup_never_gates_apply(self) -> None:
        """Scratch cleanup always runs and cannot fail the job."""
        cleanup = self.step("review", "Clear session scratch files")
        self.assertEqual(cleanup["if"], "always()")
        self.assertTrue(cleanup["continue-on-error"])


class ApplyContracts(ReusableWorkflowCase):
    """The trusted apply job: verification, bounded fetch, the one write."""

    def test_apply_runs_for_every_entry_after_review(self) -> None:
        """Failed review entries still get a verdict; cancellation stops everything."""
        job = self.jobs["apply"]
        self.assertEqual(job["needs"], ["select", "review"])
        condition = squash(str(job["if"]))
        self.assertIn("!cancelled()", condition)
        self.assertIn("needs.select.result == 'success'", condition)
        self.assertEqual(job["permissions"], {"actions": "read", "contents": "read"})

    def test_live_antagonistic_writes_serialise_per_pull_request(self) -> None:
        """Across runs, one live antagonistic applier per pull request at a
        time, whatever its head, so the round count and the post are one
        step; waiting jobs queue rather than evict each other, and every
        other leg locks on a group of its own and never waits.
        """
        concurrency = self.jobs["apply"]["concurrency"]
        self.assertEqual(
            squash(str(concurrency["group"])),
            "${{ inputs.mode == 'antagonistic' && !inputs.dry_run"
            " && format('code-review-antagonistic-apply-{0}-{1}',"
            " matrix.repository, matrix.number)"
            " || format('code-review-apply-{0}-{1}-{2}',"
            " github.run_id, github.run_attempt, matrix.key) }}",
        )
        self.assertIs(concurrency["cancel-in-progress"], False)
        self.assertEqual(concurrency["queue"], "max")

    def test_evidence_is_verified_before_the_session_is_fetched(self) -> None:
        """Trusted evidence first; the untrusted session arrives through the bounded fetch."""
        self.assertLess(
            self.position("apply", "Verify evidence bytes"),
            self.position("apply", "Fetch and accept bounded session"),
        )
        fetch = self.step("apply", "Fetch and accept bounded session")
        run = flatten(fetch["run"])
        self.assertIn("artifact_fetch.py", run)
        self.assertIn("--profile session", run)
        self.assertIn("3) failure=", run)
        self.assertIn("4) failure=", run)
        self.assertEqual(fetch["env"]["GH_TOKEN"], "${{ github.token }}")
        downloads = self.actions("apply", DOWNLOAD)
        self.assertEqual(len(downloads), 1)
        self.assertEqual(
            downloads[0]["with"]["artifact-ids"],
            "${{ needs.select.outputs.evidence_id }}",
        )

    def test_write_token_is_minted_for_one_repository_on_the_live_path(self) -> None:
        """pull-requests: write for matrix.repo_name, live, and accepted by
        the offline check of the run's own mode.
        """
        mint = self.action("apply", APP_TOKEN)
        self.assertEqual(
            squash(str(mint["if"])),
            "!inputs.dry_run && inputs.github_app_client_id != '' && ("
            " (inputs.mode == 'approve' && steps.check.outputs.approvable == 'true') ||"
            " (inputs.mode == 'antagonistic'"
            " && steps.check.outputs.commentable == 'true'))",
        )
        self.assertEqual(mint["with"]["repositories"], "${{ matrix.repo_name }}")
        self.assertEqual(mint["with"]["permission-pull-requests"], "write")
        for grant, level in mint["with"].items():
            if grant.startswith("permission-") and grant != "permission-pull-requests":
                self.assertEqual(level, "read", grant)
        self.assertLess(
            self.position("apply", "Check verdict offline"),
            self.position("apply", "Mint pull request write token"),
        )

    def test_apply_falls_back_to_the_native_token_which_cannot_approve(self) -> None:
        """Dry runs and refused verdicts re-read with GITHUB_TOKEN alone."""
        apply = self.step("apply", "Apply verdict")
        self.assertEqual(
            apply["env"]["GH_TOKEN"],
            "${{ steps.write-token.outputs.token || github.token }}",
        )
        run = flatten(apply["run"])
        self.assertIn('if [ "$DRY_RUN" = true ]; then extra+=(--dry-run); fi', run)
        self.assertIn('python3 "review-assets/scripts/${command[0]}"', run)

    def test_each_mode_checks_and_applies_with_its_own_script(self) -> None:
        """approve uses apply_review; antagonistic uses comment_review, which
        cannot approve. An unknown mode fails both steps.
        """
        check = self.step("apply", "Check verdict offline")
        self.assertEqual(check["env"]["MODE"], "${{ inputs.mode }}")
        run = flatten(check["run"])
        self.assertIn(
            "approve) python3 review-assets/scripts/apply_review.py check", run
        )
        self.assertIn(
            "antagonistic) python3 review-assets/scripts/comment_review.py check", run
        )
        self.assertIn("*) echo '::error::unknown mode'; exit 1 ;;", run)
        apply = self.step("apply", "Apply verdict")
        self.assertEqual(apply["env"]["MODE"], "${{ inputs.mode }}")
        run = flatten(apply["run"])
        self.assertIn("approve) command=(apply_review.py apply) ;;", run)
        self.assertIn(
            "antagonistic) command=(comment_review.py comment)"
            ' extra+=(--run-id "$RUN_ID")'
            ' if [ "$HANDBACK" = true ]; then extra+=(--handback); fi ;;',
            run,
        )
        self.assertEqual(
            apply["env"]["HANDBACK"], "${{ inputs.handback_repository != '' }}"
        )
        self.assertEqual(apply["env"]["RUN_ID"], "${{ github.run_id }}")
        self.assertIn("*) echo '::error::unknown mode'; exit 1 ;;", run)

    def test_every_entry_uploads_a_typed_result(self) -> None:
        """A missing result becomes a failed verdict so the report has a row."""
        fallback = self.step("apply", "Ensure a result exists")
        self.assertEqual(fallback["if"], "always()")
        run = flatten(fallback["run"])
        self.assertIn('verdict: "failed"', run)
        upload = self.action("apply", UPLOAD)
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["retention-days"], 90)
        self.assertEqual(
            upload["with"]["name"],
            "code-review-result-${{ needs.select.outputs.namespace }}"
            "-${{ matrix.key }}-${{ github.run_attempt }}",
        )


class ReportContracts(ReusableWorkflowCase):
    """The report job gathers results and publishes the next run's ledger."""

    def test_report_gathers_namespaced_results(self) -> None:
        """Only this invocation's results feed the report."""
        gather = self.step("report", "Gather results")
        self.assertEqual(
            gather["with"]["pattern"],
            "code-review-result-${{ needs.select.outputs.namespace }}-*",
        )
        run = flatten(self.step("report", "Build report and ledger")["run"])
        self.assertIn("review_report.py", run)
        self.assertIn("--ledger evidence/ledger.json", run)
        self.assertIn("--output-ledger ledger/ledger.json", run)

    def test_ledger_artifact_has_a_fixed_name_and_long_retention(self) -> None:
        """The next select job finds the mode's ledger by name across runs."""
        ledger = self.step("report", "Publish ledger")
        self.assertEqual(
            ledger["with"]["name"], "${{ needs.select.outputs.ledger_name }}"
        )
        self.assertEqual(ledger["with"]["retention-days"], 90)
        self.assertEqual(ledger["with"]["if-no-files-found"], "error")
        report = self.step("report", "Attach run report")
        self.assertEqual(report["with"]["retention-days"], 90)


class ListInputContracts(ReusableWorkflowCase):
    """Operator lists are split and checked once, before any token exists."""

    # Splitter step id and validate mode for each list input.
    LISTS: ClassVar[dict[str, tuple[str, str]]] = {
        "repositories": ("repositories", "repository"),
        "exclude_repos": ("exclusions", "repository"),
        "pull_requests": ("pulls", "pull-request"),
    }

    def test_each_list_is_split_as_objects_of_the_org(self) -> None:
        """Names and references of the trusted owner, offline, with no token."""
        self.assertEqual(len(self.actions("select", SPLITTER)), len(self.LISTS))
        for name, (identity, mode) in self.LISTS.items():
            with self.subTest(input=name):
                split = self.step("select", identity)
                self.assertTrue(is_action(split, SPLITTER))
                self.assertEqual(split["with"]["list"], f"${{{{ inputs.{name} }}}}")
                self.assertEqual(split["with"]["validate"], mode)
                self.assertEqual(split["with"]["owner"], "${{ inputs.org }}")
                self.assertEqual(split["with"]["token"], "")
                self.assertNotIn("verify", split["with"])

    def test_lists_are_split_before_the_read_mint(self) -> None:
        """A bad name or reference fails the run before the App key is used."""
        mint = self.position("select", "app-token")
        for identity, _ in self.LISTS.values():
            with self.subTest(step=identity):
                self.assertLess(self.position("select", identity), mint)

    def test_read_mint_scope_follows_the_named_targets(self) -> None:
        """Named pull requests win, then named repositories; blank scans."""
        mint = self.action("select", APP_TOKEN)
        self.assertEqual(
            squash(mint["with"]["repositories"]),
            "${{ steps.pulls.outputs.count != '0'"
            " && steps.pulls.outputs.repositories"
            " || steps.repositories.outputs.repositories }}",
        )

    def test_selection_reads_the_split_lists(self) -> None:
        """The raw inputs reach the splitters and nothing else."""
        env = self.step("select", "Select pull requests")["env"]
        self.assertEqual(env["PULL_REQUESTS"], "${{ steps.pulls.outputs.csv }}")
        self.assertEqual(env["REPOSITORIES"], "${{ steps.repositories.outputs.csv }}")
        self.assertEqual(env["EXCLUDE_REPOS"], "${{ steps.exclusions.outputs.csv }}")
        splitters = {id(s) for s in self.actions("select", SPLITTER)}
        for job in self.jobs:
            for step in self.steps(job):
                if id(step) in splitters:
                    continue
                with self.subTest(job=job, step=step.get("name")):
                    text = dumped(step)
                    for name in self.LISTS:
                        self.assertNotIn(f"inputs.{name}", text)


class InputContracts(ReusableWorkflowCase):
    """Declared inputs, secrets and their defaults."""

    def test_declared_inputs_and_secrets(self) -> None:
        """The caller contract matches the design."""
        call = self.triggers["workflow_call"]
        self.assertEqual(
            set(call["inputs"]),
            {
                "org",
                "mode",
                "dry_run",
                "model",
                "approve_tiers",
                "pull_requests",
                "repositories",
                "exclude_repos",
                "max_pull_requests",
                "max_concurrent_agents",
                "max_runtime_minutes",
                "reassess",
                "skip_agent",
                "egress_policy",
                "egress_allow_config",
                "github_app_client_id",
                "handback_repository",
                "assets_repository",
                "assets_ref",
            },
        )
        self.assertEqual(
            set(call["secrets"]),
            {"copilot_token", "github_app_private_key", "handback_token"},
        )

    def test_input_defaults(self) -> None:
        """Approve mode and dry-run by default, both tiers, block egress."""
        inputs = self.triggers["workflow_call"]["inputs"]
        self.assertEqual(inputs["mode"]["default"], "approve")
        self.assertTrue(inputs["dry_run"]["default"])
        self.assertEqual(inputs["model"]["default"], "claude-opus-5.5")
        self.assertEqual(inputs["approve_tiers"]["default"], "trivial,low-risk")
        self.assertEqual(inputs["max_pull_requests"]["default"], "20")
        self.assertEqual(inputs["egress_policy"]["default"], "block")
        self.assertFalse(inputs["reassess"]["default"])
        self.assertFalse(inputs["skip_agent"]["default"])


class CronContracts(WorkflowCase):
    """The scheduled caller."""

    WORKFLOW = CRON

    def test_schedule_runs_three_hourly_on_weekdays_and_twice_at_weekends(self) -> None:
        """Two cron lines: every three hours Mon-Fri, 08:00 and 20:00 Sat-Sun."""
        crons = [entry["cron"] for entry in self.triggers["schedule"]]
        self.assertEqual(crons, ["0 */3 * * 1-5", "0 8,20 * * 0,6"])

    def test_schedule_stays_dry_run_until_flipped(self) -> None:
        """Scheduled runs pass dry_run true; a dispatch takes the operator's choice."""
        call = self.jobs["code-review"]
        self.assertEqual(call["uses"], REUSABLE_CALL)
        self.assertEqual(call["with"]["mode"], "approve")
        self.assertEqual(
            squash(str(call["with"]["dry_run"])),
            "${{ github.event_name != 'workflow_dispatch' || inputs.dry_run }}",
        )
        self.assertEqual(
            call["with"]["approve_tiers"],
            "${{ inputs.approve_tiers || 'trivial,low-risk' }}",
        )

    def test_dispatch_form(self) -> None:
        """The form offers the operator every tunable the design names."""
        inputs = self.triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs),
            {
                "dry_run",
                "model",
                "approve_tiers",
                "pull_requests",
                "repositories",
                "max_pull_requests",
                "max_concurrent_agents",
                "reassess",
            },
        )
        self.assertTrue(inputs["dry_run"]["default"])
        self.assertEqual(inputs["model"]["default"], "Claude Opus 5.5")
        self.assertEqual(
            inputs["approve_tiers"]["options"], ["trivial,low-risk", "trivial", "none"]
        )

    def test_model_display_names_map_to_identifiers(self) -> None:
        """Every choice in the form has a case arm mapping it to a CLI identifier."""
        options = self.triggers["workflow_dispatch"]["inputs"]["model"]["options"]
        run = self.step("options", "Map display name to identifier")["run"]
        for name in options:
            self.assertIn(f"'{name}') id='", run)
        self.assertEqual(
            self.jobs["code-review"]["with"]["model"],
            "${{ needs.options.outputs.model }}",
        )

    def test_caller_forwards_credentials_and_pins_assets(self) -> None:
        """The template-named App credentials and the model PAT reach the workflow.

        Every bot repository uses the same names, so no caller names an
        App or a repository in a credential.
        """
        call = self.jobs["code-review"]
        self.assertEqual(
            call["with"]["github_app_client_id"],
            "${{ vars.BOT_APP_CLIENT_ID || '' }}",
        )
        self.assertEqual(
            call["secrets"],
            {
                "copilot_token": "${{ secrets.COPILOT_CLI_TOKEN }}",
                "github_app_private_key": "${{ secrets.BOT_APP_PRIVATE_KEY }}",
            },
        )
        self.assertEqual(call["with"]["assets_repository"], "${{ github.repository }}")
        self.assertEqual(call["with"]["assets_ref"], "${{ github.sha }}")
        self.assertEqual(call["with"]["egress_policy"], "block")
        self.assertRegex(call["with"]["egress_allow_config"], ALLOW_CONFIG)
        self.assertEqual(
            call["permissions"],
            {"pull-requests": "read", "contents": "read", "actions": "read"},
        )

    def test_concurrency_keeps_dry_dispatches_out_of_the_live_group(self) -> None:
        """A dry dispatch never queues behind or cancels the schedule."""
        group = squash(str(self.workflow["concurrency"]["group"]))
        self.assertIn("inputs.dry_run", group)
        self.assertIn("'code-review'", group)
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])

    def test_options_job_blocks_egress_with_the_allow_list(self) -> None:
        """The options job loads the caller's allow-list, then blocks with it.

        Without the loader, harden-runner in block mode configures no
        egress rules at all. The loader reads the public ``.github``
        repository, so the job keeps an empty permission set.
        """
        self.assertEqual(self.jobs["options"]["permissions"], {})
        steps = self.steps("options")
        first_action = next(s for s in steps if "uses" in s)
        self.assertTrue(is_action(first_action, BLOCK_ACTION))
        loader = self.action("options", BLOCK_ACTION)
        self.assertEqual(
            loader["with"]["config"],
            self.jobs["code-review"]["with"]["egress_allow_config"],
        )
        self.assertEqual(loader["with"]["allow_list_summary"], "false")
        harden = self.action("options", HARDEN_RUNNER)
        self.assertLess(steps.index(loader), steps.index(harden))
        self.assertEqual(harden["with"]["egress-policy"], "block")
        self.assertEqual(
            harden["with"]["allowed-endpoints"], "${{ env.CONNECTION_ALLOW_LIST }}"
        )

    def test_caller_steps_are_pinned(self) -> None:
        """The options job's actions name commits."""
        self.assert_pinned_steps()


class TestingContracts(WorkflowCase):
    """The pull request plumbing, the manual dry-run and the regression job."""

    WORKFLOW = TESTING

    def test_plumbing_is_secretless_selection_of_the_pr_head(self) -> None:
        """PR runs skip the agent, pass no secrets, and run the PR head's assets."""
        plumbing = self.jobs["plumbing"]
        self.assertEqual(plumbing["uses"], REUSABLE_CALL)
        self.assertEqual(plumbing["if"], "github.event_name == 'pull_request'")
        self.assertNotIn("secrets", plumbing)
        self.assertTrue(plumbing["with"]["dry_run"])
        self.assertTrue(plumbing["with"]["skip_agent"])
        self.assertEqual(
            squash(str(plumbing["with"]["assets_repository"])),
            "${{ github.event.pull_request.head.repo.full_name }}",
        )
        self.assertEqual(
            plumbing["with"]["assets_ref"], "${{ github.event.pull_request.head.sha }}"
        )
        self.assertEqual(
            plumbing["strategy"]["matrix"]["mode"], ["approve", "antagonistic"]
        )
        self.assertEqual(plumbing["with"]["mode"], "${{ matrix.mode }}")
        self.assertEqual(
            squash(str(plumbing["with"]["approve_tiers"])),
            "${{ matrix.mode == 'antagonistic' && 'none' || 'trivial,low-risk' }}",
        )

    def test_manual_dry_run_holds_the_model_credential_alone(self) -> None:
        """The dispatch path runs the agent dry with no App key."""
        dry = self.jobs["dry-run"]
        self.assertEqual(dry["if"], "github.event_name == 'workflow_dispatch'")
        self.assertTrue(dry["with"]["dry_run"])
        self.assertEqual(
            dry["secrets"], {"copilot_token": "${{ secrets.COPILOT_CLI_TOKEN }}"}
        )
        self.assertNotIn("github_app_client_id", dry["with"])
        self.assertEqual(
            dry["with"]["pull_requests"], "${{ inputs.pull_requests || '' }}"
        )

    def test_regression_runs_the_locked_unittest_suite(self) -> None:
        """The offline suite runs from the lockfile with read-only permissions."""
        regression = self.jobs["regression"]
        self.assertEqual(regression["permissions"], {"contents": "read"})
        run = self.step("regression", "Run offline regression suite")["run"]
        self.assertIn("uv run --locked python -B -m unittest discover -s tests", run)

    def test_caller_steps_are_pinned(self) -> None:
        """Runner steps name commits."""
        self.assert_pinned_steps()


class AntagonisticCallerContracts(WorkflowCase):
    """The antagonistic caller: dispatch only, its own App, never an approval."""

    WORKFLOW = ANTAGONISTIC

    def test_dispatch_only_with_gpt_by_default(self) -> None:
        """No schedule; the form defaults to a dry run on GPT-6 Astra, hand-back on."""
        self.assertEqual(set(self.triggers), {"workflow_dispatch"})
        inputs = self.triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs),
            {
                "dry_run",
                "model",
                "pull_requests",
                "repositories",
                "max_pull_requests",
                "max_concurrent_agents",
                "reassess",
                "handback",
            },
        )
        self.assertTrue(inputs["dry_run"]["default"])
        self.assertIs(inputs["handback"]["default"], True)
        self.assertEqual(inputs["model"]["default"], "GPT-6 Astra")
        self.assertNotIn("approve_tiers", inputs)

    def test_model_display_names_map_to_identifiers(self) -> None:
        """Every choice in the form has a case arm mapping it to a CLI identifier."""
        options = self.triggers["workflow_dispatch"]["inputs"]["model"]["options"]
        run = self.step("options", "Map display name to identifier")["run"]
        for name in options:
            self.assertIn(f"'{name}') id='", run)
        self.assertIn("'GPT-6 Astra') id='gpt-6-astra' ;;", run)

    def test_runs_the_antagonistic_mode_and_never_approves(self) -> None:
        """The mode and an empty tier set are literals the dispatch cannot change."""
        call = self.jobs["code-review"]
        self.assertEqual(call["uses"], REUSABLE_CALL)
        self.assertEqual(call["with"]["mode"], "antagonistic")
        self.assertEqual(call["with"]["approve_tiers"], "none")
        self.assertEqual(call["with"]["model"], "${{ needs.options.outputs.model }}")
        self.assertEqual(call["with"]["dry_run"], "${{ inputs.dry_run }}")

    def test_holds_the_antagonistic_app_alone(self) -> None:
        """The approving App's names appear nowhere in this caller."""
        call = self.jobs["code-review"]
        self.assertEqual(
            call["with"]["github_app_client_id"],
            "${{ vars.ANTAGONISTIC_APP_CLIENT_ID || '' }}",
        )
        self.assertEqual(
            call["secrets"],
            {
                "copilot_token": "${{ secrets.COPILOT_CLI_TOKEN }}",
                "github_app_private_key": "${{ secrets.ANTAGONISTIC_APP_PRIVATE_KEY }}",
                "handback_token": "${{ secrets.CODE_MONKEY_DISPATCH_TOKEN }}",
            },
        )
        self.assertEqual(
            squash(str(call["with"]["handback_repository"])),
            "${{ inputs.handback"
            " && format('{0}/github-code-monkey-bot', github.repository_owner)"
            " || '' }}",
        )
        text = dumped(self.jobs)
        self.assertNotIn("BOT_APP_CLIENT_ID", text)
        self.assertNotIn("BOT_APP_PRIVATE_KEY", text)
        self.assertEqual(call["with"]["assets_repository"], "${{ github.repository }}")
        self.assertEqual(call["with"]["assets_ref"], "${{ github.sha }}")
        self.assertEqual(call["with"]["egress_policy"], "block")
        self.assertRegex(call["with"]["egress_allow_config"], ALLOW_CONFIG)
        self.assertEqual(
            call["permissions"],
            {"pull-requests": "read", "contents": "read", "actions": "read"},
        )

    def test_each_dispatch_locks_within_its_own_run(self) -> None:
        """A newer dispatch never cancels a pending batch."""
        group = squash(str(self.workflow["concurrency"]["group"]))
        self.assertIn("code-review-antagonistic-{0}', github.run_id", group)
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])
        self.assertEqual(self.workflow["permissions"], {})

    def test_options_job_blocks_egress_with_the_allow_list(self) -> None:
        """The options job loads the caller's allow-list, then blocks with it."""
        self.assertEqual(self.jobs["options"]["permissions"], {})
        loader = self.action("options", BLOCK_ACTION)
        self.assertEqual(
            loader["with"]["config"],
            self.jobs["code-review"]["with"]["egress_allow_config"],
        )
        harden = self.action("options", HARDEN_RUNNER)
        self.assertEqual(harden["with"]["egress-policy"], "block")

    def test_caller_steps_are_pinned(self) -> None:
        """The options job's actions name commits."""
        self.assert_pinned_steps()


class ApprovingCallerContracts(WorkflowCase):
    """The approving caller never holds the antagonistic App's names."""

    WORKFLOW = CRON

    def test_holds_the_approving_app_alone(self) -> None:
        """Neither of the antagonistic App's names appears in the cron caller."""
        text = dumped(self.jobs)
        self.assertNotIn("ANTAGONISTIC_APP", text)


class HandbackContracts(ReusableWorkflowCase):
    """The hand-back: one job, one credential, one fixed dispatch target."""

    def test_report_lists_the_hand_backs_from_its_own_report(self) -> None:
        """The trusted report job derives the list, from markers naming this
        run alone; the hand-back job reads none.
        """
        step = self.step("report", "List hand-backs")
        self.assertIn(
            "handback.py list --report artefacts/report.json"
            " --selection evidence/selection.json"
            ' --org "$ORG" --run-id "$RUN_ID" >> "$GITHUB_OUTPUT"',
            flatten(step["run"]),
        )
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(step["env"]["RUN_ID"], "${{ github.run_id }}")
        self.assertLess(
            self.position("report", "Build report and ledger"),
            self.position("report", "List hand-backs"),
        )
        outputs = self.jobs["report"]["outputs"]
        self.assertEqual(
            outputs["handback_pulls"], "${{ steps.handback.outputs.pulls }}"
        )
        self.assertEqual(
            outputs["handback_count"], "${{ steps.handback.outputs.count }}"
        )

    def test_hand_back_runs_live_antagonistic_runs_with_work_alone(self) -> None:
        """Never in a dry run, never in the approve mode, never without a target."""
        job = self.jobs["handback"]
        self.assertEqual(job["needs"], ["select", "report"])
        self.assertEqual(
            squash(str(job["if"])),
            "!cancelled() && needs.report.result == 'success' &&"
            " !inputs.dry_run && inputs.mode == 'antagonistic' &&"
            " inputs.handback_repository != '' &&"
            " needs.report.outputs.handback_count != '0' &&"
            " needs.report.outputs.handback_count != ''",
        )
        self.assertEqual(job["permissions"], {})
        self.assertEqual(self.actions("handback", CHECKOUT), [])
        self.assertEqual(self.actions("handback", DOWNLOAD), [])

    def test_the_token_reaches_the_dispatch_step_alone(self) -> None:
        """handback_token appears in one step of one job, and nowhere else."""
        for job, body in self.jobs.items():
            refs = SECRET_REFERENCE.findall(dumped(body))
            with self.subTest(job=job):
                if job == "handback":
                    self.assertEqual(refs, ["secrets.handback_token"])
                else:
                    self.assertNotIn("secrets.handback_token", refs)
        step = self.step("handback", "Dispatch the revision round")
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ secrets.handback_token }}")

    def test_dispatch_target_and_list_are_fixed_and_checked(self) -> None:
        """One workflow file, live, revision mode, no count cap, and a validated
        list dispatched whole in chunks of fifty.
        """
        step = self.step("handback", "Dispatch the revision round")
        run = flatten(step["run"])
        self.assertIn(
            'gh workflow run code-monkey-cron.yaml --repo "$REPOSITORY"'
            " -f dry_run=false -f mode=pull-requests -f max_pull_requests=0"
            ' -f pull_requests="$chunk"',
            run,
        )
        self.assertIn("for ((start = 0; start < ${#all[@]}; start += 50)); do", run)
        self.assertIn('chunk="${all[*]:start:50}"', run)
        self.assertIn('[[ "$REPOSITORY" =~ $repo_re ]]', run)
        self.assertIn('[[ "$PULLS" =~ ^$ref(\\ $ref)*$ ]]', run)
        self.assertEqual(
            step["env"]["PULLS"], "${{ needs.report.outputs.handback_pulls }}"
        )

    def test_the_approve_callers_hand_nothing_back(self) -> None:
        """Neither the schedule nor the testing workflow names a target or token."""
        for path in (CRON, TESTING):
            loaded = cast(dict[str | bool, Any], yaml.safe_load(path.read_text()))
            text = dumped(loaded["jobs"])
            with self.subTest(workflow=path.name):
                self.assertNotIn("handback", text)


class CommentPathContracts(unittest.TestCase):
    """A lexical tripwire over the antagonistic apply path.

    It scans the string literals of the modules the path owns for the
    two review events it must never send, and their imports for the
    approving applier. It catches an honest mistake, not a determined
    one: a split or computed string passes, and the other modules the
    applier loads are not scanned. The enforcement is behavioural: the
    applier's ``post`` refuses any event but COMMENT at the call, and
    ``test_comment_review`` asserts COMMENT on every write it sees.
    """

    def test_no_other_review_event_in_the_comment_path(self) -> None:
        """Neither APPROVE nor REQUEST_CHANGES is a string in those modules."""
        for name in COMMENT_PATH:
            constants = string_constants(module_tree(name))
            for event in FORBIDDEN_EVENTS:
                with self.subTest(module=name, event=event):
                    self.assertFalse(any(event in text for text in constants))

    def test_the_comment_path_never_imports_the_approver(self) -> None:
        """apply_review, which approves, stays out of the comment path."""
        for name in COMMENT_PATH:
            with self.subTest(module=name):
                self.assertNotIn("apply_review", imported_modules(module_tree(name)))

    def test_the_one_event_is_comment(self) -> None:
        """comment_review names COMMENT as its event and posts no other."""
        tree = module_tree("comment_review")
        events = [
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "EVENT"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ]
        self.assertEqual(events, ["COMMENT"])


if __name__ == "__main__":
    unittest.main()
