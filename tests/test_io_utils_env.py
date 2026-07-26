"""Environment-file helper tests."""

from butly_core.io_utils import remove_env_vars, upsert_env_var


class TestUpsertEnvVar:
    def test_creates_file_and_parent_directory(self, tmp_path):
        env_path = tmp_path / "nested" / ".env"

        upsert_env_var(env_path, "NANOGPT_API_KEY", "secret")

        assert env_path.read_text(encoding="utf-8") == (
            "NANOGPT_API_KEY=secret\n"
        )

    def test_preserves_comments_blank_lines_and_unrelated_values(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# Butly keys\n"
            "OPENAI_API_KEY=keep-me\n"
            "\n"
            "# NanoGPT\n"
            "NANOGPT_API_KEY=old\n"
            "CUSTOM_VALUE=a=b\n",
            encoding="utf-8",
        )

        upsert_env_var(env_path, "NANOGPT_API_KEY", "new-value")

        assert env_path.read_text(encoding="utf-8") == (
            "# Butly keys\n"
            "OPENAI_API_KEY=keep-me\n"
            "\n"
            "# NanoGPT\n"
            "NANOGPT_API_KEY=new-value\n"
            "CUSTOM_VALUE=a=b\n"
        )

    def test_replaces_first_occurrence_and_removes_duplicates(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "NANOGPT_API_KEY=first\n"
            "OTHER=value\n"
            "  NANOGPT_API_KEY = duplicate\n",
            encoding="utf-8",
        )

        upsert_env_var(env_path, "NANOGPT_API_KEY", "replacement")

        assert env_path.read_text(encoding="utf-8") == (
            "NANOGPT_API_KEY=replacement\n"
            "OTHER=value\n"
        )


class TestRemoveEnvVars:
    def test_removes_requested_variables_and_preserves_formatting(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# API keys\n"
            "GEMINI_API_KEY=remove-primary\n"
            "\n"
            "OPENAI_API_KEY=keep-me\n"
            "  GOOGLE_API_KEY=remove-fallback\n"
            "# End\n",
            encoding="utf-8",
        )

        changed = remove_env_vars(
            env_path,
            ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        )

        assert changed is True
        assert env_path.read_text(encoding="utf-8") == (
            "# API keys\n"
            "\n"
            "OPENAI_API_KEY=keep-me\n"
            "# End\n"
        )

    def test_returns_false_without_rewriting_when_no_match(self, tmp_path):
        env_path = tmp_path / ".env"
        original = "# Existing\nOPENAI_API_KEY=keep\n"
        env_path.write_text(original, encoding="utf-8")

        changed = remove_env_vars(env_path, ("NANOGPT_API_KEY",))

        assert changed is False
        assert env_path.read_text(encoding="utf-8") == original

    def test_returns_false_when_file_does_not_exist(self, tmp_path):
        env_path = tmp_path / ".env"

        assert remove_env_vars(env_path, ("NANOGPT_API_KEY",)) is False
        assert not env_path.exists()

    def test_removing_all_variables_leaves_empty_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "NANOGPT_API_KEY=first\nNANOGPT_API_KEY=duplicate\n",
            encoding="utf-8",
        )

        changed = remove_env_vars(env_path, ("NANOGPT_API_KEY",))

        assert changed is True
        assert env_path.read_text(encoding="utf-8") == ""
