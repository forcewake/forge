from forge.context.redactor import Redactor


class TestRedactor:
    def setup_method(self):
        self.redactor = Redactor()

    def test_clean_text_unchanged(self):
        text = "def hello():\n    return 'world'\n"
        assert self.redactor.redact(text) == text

    def test_aws_access_key(self):
        text = "aws_key = AKIAIOSFODNN7EXAMPLE"
        result = self.redactor.redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED aws_key]" in result

    def test_aws_secret_key(self):
        text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result = self.redactor.redact(text)
        assert "wJalrXUtnFEMI" not in result
        assert "[REDACTED aws_secret]" in result

    def test_gitlab_pat(self):
        text = "GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx"
        result = self.redactor.redact(text)
        assert "glpat-" not in result
        assert "[REDACTED gitlab_token]" in result

    def test_github_token(self):
        text = "token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        result = self.redactor.redact(text)
        assert "ghp_" not in result
        assert "[REDACTED github_token]" in result

    def test_generic_sk_token(self):
        text = "api_key = sk-abcdefghij1234567890xxxx"
        result = self.redactor.redact(text)
        assert "sk-abcdefghij" not in result
        assert "REDACTED" in result

    def test_password_redaction(self):
        text = "password = super_secret_123"
        result = self.redactor.redact(text)
        assert "super_secret_123" not in result
        assert "[REDACTED password]" in result

    def test_private_key_block(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogIBAAJBALz...\n"
            "base64data==\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        result = self.redactor.redact(text)
        assert "MIIBogIBAAJBALz" not in result
        assert "[REDACTED private_key]" in result

    def test_connection_string(self):
        text = "DATABASE_URL=postgres://admin:s3cret@db.host:5432/mydb"
        result = self.redactor.redact(text)
        assert "s3cret" not in result
        assert "[REDACTED connection_string]" in result

    def test_extra_patterns(self):
        redactor = Redactor(
            extra_patterns=[{"name": "internal_key", "pattern": r"INTERNAL-[A-Z]{10,}"}]
        )
        text = "key = INTERNAL-ABCDEFGHIJKLMNO"
        result = redactor.redact(text)
        assert "INTERNAL-ABCDEFGHIJKLMNO" not in result
        assert "[REDACTED internal_key]" in result

    def test_entropy_detection(self):
        # High-entropy string in assignment context
        text = "SECRET_KEY = a8f3k2m9x7q1w4e6r0t5y8u2i3o7p6"
        result = self.redactor.redact(text)
        assert "[REDACTED high-entropy]" in result

    def test_entropy_normal_value_passes(self):
        # Low-entropy string should not be flagged
        text = "NAME = aaaaaaaaaaaaaaaa"
        result = self.redactor.redact(text)
        assert result == text

    def test_multiple_secrets_in_one_text(self):
        text = (
            "GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx\n"
            "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            "clean line here\n"
        )
        result = self.redactor.redact(text)
        assert "glpat-" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "clean line here" in result
