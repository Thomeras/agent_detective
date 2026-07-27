"""Judge selection: when the judged channel is on, and when it honestly is not."""

from __future__ import annotations

import asyncio

import pytest
from worker.config import Settings
from worker.judge_client import OpenAIJudgeClient, PermanentJudgeError

from detective_cli.judge import NullJudge, judge_configured, select_judge


class TestConfiguration:
    def test_the_placeholder_default_does_not_count_as_configured(self):
        # Settings' default points at a local port nothing listens on. Reading
        # that as "there is a judge" would make every default run wait on
        # connection refusals and then report a judge failure.
        assert judge_configured(Settings()) is False

    def test_an_explicit_endpoint_counts_as_configured(self):
        settings = Settings(judge_base_url="http://localhost:11434/v1")
        assert judge_configured(settings) is True

    def test_an_empty_base_url_is_not_configured(self):
        assert judge_configured(Settings(judge_base_url="")) is False

    def test_whitespace_is_not_a_base_url(self):
        assert judge_configured(Settings(judge_base_url="   ")) is False


class TestSelection:
    def test_no_configuration_yields_the_null_judge(self):
        choice = select_judge(Settings())
        assert isinstance(choice.client, NullJudge)
        assert choice.enabled is False
        assert "not configured" in choice.description

    def test_a_configured_endpoint_yields_a_real_client(self):
        choice = select_judge(Settings(judge_base_url="http://x/v1", judge_model="m"))
        assert isinstance(choice.client, OpenAIJudgeClient)
        assert choice.enabled is True
        assert "m" in choice.description

    def test_force_off_beats_a_configured_endpoint(self):
        choice = select_judge(
            Settings(judge_base_url="http://x/v1", judge_model="m"), force_off=True
        )
        assert isinstance(choice.client, NullJudge)
        assert choice.enabled is False
        assert "--no-judge" in choice.description

    def test_closing_a_choice_is_safe_for_every_client(self):
        asyncio.run(select_judge(Settings()).close())
        asyncio.run(
            select_judge(Settings(judge_base_url="http://x/v1", judge_model="m")).close()
        )


class TestNullJudge:
    def test_it_fails_permanently_rather_than_returning_a_verdict(self):
        # Returning a passing score here would manufacture the exact false
        # confidence the project exists to prevent.
        with pytest.raises(PermanentJudgeError):
            asyncio.run(NullJudge().complete_json("any prompt"))

    def test_the_failure_says_the_channel_is_off(self):
        with pytest.raises(PermanentJudgeError, match="judged channel is off"):
            asyncio.run(NullJudge().complete_json("any prompt"))
