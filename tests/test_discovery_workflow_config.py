from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "run.yml"


def _job_block(text: str, name: str, next_name: str | None) -> str:
    start = text.index(f"  {name}:\n")
    end = len(text) if next_name is None else text.index(f"  {next_name}:\n", start)
    return text[start:end]


def test_remote_discovery_has_two_dependent_least_privilege_jobs():
    text = WORKFLOW.read_text(encoding="utf-8")
    ingest = _job_block(text, "ingest-and-screen", "deep-grade")
    grade = _job_block(text, "deep-grade", "handle-opportunity-decision")

    assert "cron: '0 6 * * *'" in text
    assert "actions: read" in text
    assert "OPENAI_API_KEY" not in ingest
    assert "ANTHROPIC_API_KEY" not in ingest
    assert "JOB_AGENT_GRADING_PROFILE_JSON" not in ingest
    assert "uses: actions/upload-artifact@v4" in ingest
    assert "shortlist-${{ github.run_id }}-${{ github.run_attempt }}" in ingest
    assert "python -m actions_state restore-latest" in ingest
    assert "python -m actions_state write-manifest" in ingest
    assert "--stage ingest" in ingest
    assert (
        "discovery-state-ingest-${{ github.run_id }}-${{ github.run_attempt }}"
        in ingest
    )
    assert "python -m discovery_jobs ingest-and-screen" in ingest
    assert "python -m discovery_jobs finalize-ingest" in ingest
    assert "python -m discovery_jobs dispatch-schedule" in ingest
    assert "needs: ingest-and-screen" in grade
    assert "needs.ingest-and-screen.outputs.verified_count != '0'" in grade
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in grade
    assert "JOB_AGENT_GRADING_PROFILE_JSON" in grade
    assert "uses: actions/download-artifact@v4" in grade
    assert "needs.ingest-and-screen.outputs.shortlist_artifact" in grade
    assert "python -m actions_state validate-manifest" in grade
    assert "--stage deep" in grade
    assert (
        "discovery-state-deep-${{ github.run_id }}-${{ github.run_attempt }}"
        in grade
    )
    assert "python -m discovery_jobs deep-grade" in grade
    assert "python -m discovery_jobs dispatch-schedule" in grade
    assert "--stage-only" not in ingest
    assert "--stage-only" in grade
    assert "--dispatch-only" in grade
    for binding in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ACTOR_ID",
        "JOB_AGENT_CALLBACK_GATEWAY_URL",
        "JOB_AGENT_CALLBACK_GATEWAY_TOKEN",
    ):
        assert binding in ingest
        assert binding in grade


def test_repository_dispatch_completes_preparation_and_notifies_from_cloud():
    text = WORKFLOW.read_text(encoding="utf-8")
    prepare = _job_block(text, "prepare-artifacts", None)

    assert "repository_dispatch:" in text
    assert "prepare-application" in text
    assert (
        "format('prepare-application|{0}|{1}', "
        "github.event.client_payload.application_id, "
        "github.event.client_payload.official_vacancy_version)"
    ) in text
    assert "github.event_name != 'repository_dispatch'" in text
    assert "github.event.action == 'prepare-application'" in prepare
    assert "github.event.action == 'telegram-opportunity-decision'" in prepare
    assert "github.event.client_payload.action == 'prepare'" in prepare
    assert "client_payload.application_id" in prepare
    assert "client_payload.official_vacancy_version" in prepare
    assert "JOB_AGENT_ARTIFACT_HANDOFF_KEY" in prepare
    assert "ANTHROPIC_API_KEY" in prepare
    assert "python -m hosted_artifact_preparation prepare" in prepare
    assert "application-artifacts.enc" in prepare
    assert (
        "APPLICATION_ID: ${{ github.event.client_payload.application_id }}"
        in prepare
    )
    assert (
        "VACANCY_VERSION: ${{ github.event.client_payload.official_vacancy_version }}"
        in prepare
    )
    assert (
        "JOB_AGENT_CANONICAL_CV_URL: "
        "${{ vars.JOB_AGENT_CANONICAL_CV_URL }}" in prepare
    )
    assert 'curl -fsSL "$JOB_AGENT_CANONICAL_CV_URL"' in prepare
    assert (
        "JOB_AGENT_CANDIDATE_NAME: ${{ vars.JOB_AGENT_CANDIDATE_NAME }}"
        in prepare
    )
    assert '--candidate-name "$JOB_AGENT_CANDIDATE_NAME"' in prepare
    assert "Synthetic Owner" not in prepare
    assert '--application-id "$APPLICATION_ID"' in prepare
    assert '--official-vacancy-version "$VACANCY_VERSION"' in prepare
    assert (
        '--application-id "${{ github.event.client_payload.application_id }}"'
        not in prepare
    )
    assert "curriculum_vitae.pdf" not in prepare.split(
        "uses: actions/upload-artifact@v4"
    )[-1]
    assert "python -m hosted_preparation_completion arm" in prepare
    assert "python -m hosted_preparation_completion dispatch" in prepare
    assert "id: completion_delivery" in prepare
    assert "steps.completion_delivery.outputs.should_send == 'true'" in prepare
    assert '--claim-token "$COMPLETION_CLAIM_TOKEN"' in prepare
    assert "TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}" in prepare
    assert "TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}" in prepare
    assert "--stage prepare" in prepare
    assert "discovery-state-prepare-staged-" in prepare
    assert "discovery-state-prepare-final-" in prepare
    assert prepare.index("Publish encrypted application package") < prepare.index(
        "Stage remote preparation completion"
    )
    assert prepare.index(
        "Publish authoritative completion state before notification"
    ) < prepare.index("Notify that CV and letter are ready")
    assert prepare.index("Notify that CV and letter are ready") < prepare.index(
        "Publish final authoritative completion state"
    )


def test_hosted_workflow_supplies_owner_neutral_runtime_identity():
    text = WORKFLOW.read_text(encoding="utf-8")
    ingest = _job_block(text, "ingest-and-screen", "deep-grade")

    assert (
        "JOB_AGENT_CAREER_GMAIL: ${{ vars.JOB_AGENT_CAREER_GMAIL }}"
        in ingest
    )
    assert "JOB_AGENT_GITHUB_REPOSITORY: ${{ github.repository }}" in ingest
    assert (
        "JOB_AGENT_GITHUB_BRANCH: "
        "${{ github.event.repository.default_branch }}" in ingest
    )
    assert "Synthetic Owner" not in text
    assert "synthetic-owner" not in text


def test_hosted_workflow_materializes_private_preferences_before_ingest():
    text = WORKFLOW.read_text(encoding="utf-8")
    ingest = _job_block(text, "ingest-and-screen", "deep-grade")

    assert (
        "JOB_AGENT_PREFERENCES_YAML: "
        "${{ secrets.JOB_AGENT_PREFERENCES_YAML }}" in ingest
    )
    assert (
        'printf \'%s\' "$JOB_AGENT_PREFERENCES_YAML" '
        "> data/hosted-inputs/preferences.yaml" in ingest
    )
    assert (
        "JOB_AGENT_PREFERENCES_PATH: "
        "data/hosted-inputs/preferences.yaml" in ingest
    )


def test_hosted_workflow_materializes_private_preferences_before_deep_grade():
    text = WORKFLOW.read_text(encoding="utf-8")
    grade = _job_block(text, "deep-grade", "handle-opportunity-decision")

    assert (
        "JOB_AGENT_PREFERENCES_YAML: "
        "${{ secrets.JOB_AGENT_PREFERENCES_YAML }}" in grade
    )
    assert (
        'printf \'%s\' "$JOB_AGENT_PREFERENCES_YAML" '
        "> data/hosted-inputs/preferences.yaml" in grade
    )
    assert (
        "JOB_AGENT_PREFERENCES_PATH: "
        "data/hosted-inputs/preferences.yaml" in grade
    )
    assert (
        "rm -f data/hosted-inputs/preferences.yaml" in grade
    )


def test_deep_state_is_published_before_a_candidate_can_dispatch_preparation():
    text = WORKFLOW.read_text(encoding="utf-8")
    grade = _job_block(text, "deep-grade", "handle-opportunity-decision")

    assert grade.index("Publish authoritative graded state") < grade.index(
        "Send immediate alerts and due digest"
    )
    assert grade.index("--stage-only") < grade.index(
        "Publish authoritative graded state"
    )
    assert grade.index("Publish authoritative graded state") < grade.index(
        "--dispatch-only"
    )


def test_cloud_decisions_restore_exact_state_and_persist_discards():
    text = WORKFLOW.read_text(encoding="utf-8")
    decision = _job_block(
        text, "handle-opportunity-decision", "prepare-artifacts"
    )

    assert "telegram-opportunity-decision" in text
    assert "client_payload.action != 'prepare'" in decision
    assert "python -m actions_state restore-latest" in decision
    assert "python -m hosted_opportunity_decision" in decision
    assert "--action \"$DECISION_ACTION\"" in decision
    assert "DISCARD_REASON:" not in decision
    assert '--event-path "$GITHUB_EVENT_PATH"' in decision
    assert "python -m actions_state write-manifest" in decision
    assert "discovery-state-decision-" in decision
    assert "group: job-agent-authoritative-state" in text
