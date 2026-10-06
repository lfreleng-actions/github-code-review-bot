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
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml  # pyright: ignore[reportMissingModuleSource]

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REUSABLE = WORKFLOWS / "code-review.yaml"
CRON = WORKFLOWS / "code-review-cron.yaml"
TESTING = WORKFLOWS / "testing.yaml"

# PyYAML reads the bare mapping key ``on`` as the boolean ``True``.
TRIGGERS = True

HARDEN_RUNNER = "step-security/harden-runner"
BLOCK_ACTION = "lfreleng-actions/harden-runner-block-action"
CHECKOUT = "actions/checkout"
APP_TOKEN = "actions/create-github-app-token"
DOWNLOAD = "actions/download-artifact"
UPLOAD = "actions/upload-artifact"
REUSABLE_CALL = "$/.github/workflows/code-review.yaml"
TRUSTED_JOBS = ("select", "apply", "report")

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
        """Live runs lock per caller and owner; dry runs lock within their run."""
        group = squash(str(self.workflow["concurrency"]["group"]))
        self.assertIn("inputs.dry_run", group)
        self.assertIn("code-review-dry-run-{0}-{1}', github.run_id", group)
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

    def test_read_token_is_read_only_and_scoped_to_named_targets(self) -> None:
        """The scan token reads alone; naming targets narrows its repositories."""
        mint = self.action("select", APP_TOKEN)
        grants = {k: v for k, v in mint["with"].items() if k.startswith("permission-")}
        self.assertEqual(set(grants.values()), {"read"})
        self.assertEqual(
            mint["with"]["repositories"], "${{ steps.budget.outputs.repositories }}"
        )

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

    def test_selection_forwards_every_scoping_input(self) -> None:
        """Named pull requests, repositories, tiers and the cap reach the script."""
        run = flatten(self.step("select", "Select pull requests")["run"])
        for flag in (
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
        self.assertIn("--config review-assets/config/bot.json", run)
        self.assertIn(
            '--workflow-sha "$JOB_WORKFLOW_SHA" --assets-sha "$ASSETS_SHA"', run
        )

    def test_minted_tokens_are_checked_for_identity(self) -> None:
        """Both mints hand their app-slug to the identity check before use."""
        read = self.step("select", "Pre-flight: App identity and token grants")
        self.assertEqual(
            read["env"]["MINTED_SLUG"], "${{ steps.app-token.outputs.app-slug }}"
        )
        self.assertEqual(
            read["env"]["GH_TOKEN"], "${{ steps.app-token.outputs.token }}"
        )
        self.assertIn("preflight.py token", flatten(read["run"]))
        self.assertLess(
            self.position("select", "Pre-flight: App identity and token grants"),
            self.position("select", "Fetch prior ledger"),
        )
        write = self.step("apply", "Pre-flight: write token identity")
        self.assertEqual(
            write["env"]["MINTED_SLUG"], "${{ steps.write-token.outputs.app-slug }}"
        )
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
        read = self.action("select", APP_TOKEN)
        self.assertEqual(
            read["with"]["repositories"], "${{ steps.budget.outputs.repositories }}"
        )


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
        verify = self.step("review", "Verify evidence bytes")
        self.assertIn("review_evidence.py verify", flatten(verify["run"]))
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
        """pull-requests: write for matrix.repo_name, only when approvable and live."""
        mint = self.action("apply", APP_TOKEN)
        condition = squash(str(mint["if"]))
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("steps.check.outputs.approvable == 'true'", condition)
        self.assertIn("inputs.github_app_client_id != ''", condition)
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
        self.assertIn("apply_review.py apply", run)

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
        """The next select job finds the ledger by name across runs."""
        ledger = self.step("report", "Publish ledger")
        self.assertEqual(ledger["with"]["name"], "code-review-ledger")
        self.assertEqual(ledger["with"]["retention-days"], 90)
        self.assertEqual(ledger["with"]["if-no-files-found"], "error")
        report = self.step("report", "Attach run report")
        self.assertEqual(report["with"]["retention-days"], 90)


class InputContracts(ReusableWorkflowCase):
    """Declared inputs, secrets and their defaults."""

    def test_declared_inputs_and_secrets(self) -> None:
        """The caller contract matches the design."""
        call = self.triggers["workflow_call"]
        self.assertEqual(
            set(call["inputs"]),
            {
                "org",
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
                "assets_repository",
                "assets_ref",
            },
        )
        self.assertEqual(
            set(call["secrets"]), {"copilot_token", "github_app_private_key"}
        )

    def test_input_defaults(self) -> None:
        """Dry-run by default, both tiers approvable, block egress on trusted jobs."""
        inputs = self.triggers["workflow_call"]["inputs"]
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
            plumbing["strategy"]["matrix"]["invocation"], ["first", "second"]
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


if __name__ == "__main__":
    unittest.main()
