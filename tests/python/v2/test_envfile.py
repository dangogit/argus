import os

from argus.v2 import envfile


def test_parse_assignment_supports_export_quotes_and_comments():
    assert envfile.parse_assignment("export A='one two' # comment") == ("A", "one two")
    assert envfile.parse_assignment('B="three four"') == ("B", "three four")
    assert envfile.parse_assignment("C=plain") == ("C", "plain")
    assert envfile.parse_assignment("# ignored") is None


def test_parse_assignment_expands_home_only(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/example")
    assert envfile.parse_assignment('CODEX_HOME="$HOME/.codex-fleet"') == (
        "CODEX_HOME",
        "/Users/example/.codex-fleet",
    )
    assert envfile.parse_assignment("CODEX_HOME=~/.codex-fleet") == (
        "CODEX_HOME",
        "/Users/example/.codex-fleet",
    )
    assert envfile.parse_assignment("TOKEN=abc$NOT_HOME") == ("TOKEN", "abc$NOT_HOME")


def test_load_env_files_preserves_existing_env_and_sets_aliases(tmp_path, monkeypatch):
    p = tmp_path / "runtime.env"
    p.write_text(
        "A=file\n"
        "B=from-file\n"
        "GITHUB_TOKEN=tok\n"
        "EVOLUTION_API_KEY=wa-key\n"
        "EVOLUTION_URL=http://127.0.0.1:8080\n"
        "EVOLUTION_INSTANCE=hq\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("A", "process")
    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("ARGUS_WA_APIKEY", raising=False)
    monkeypatch.delenv("ARGUS_WA_URL", raising=False)
    monkeypatch.delenv("ARGUS_WA_INSTANCE", raising=False)
    monkeypatch.delenv("EVOLUTION_API_KEY", raising=False)
    monkeypatch.delenv("EVOLUTION_URL", raising=False)
    monkeypatch.delenv("EVOLUTION_API_URL", raising=False)
    monkeypatch.delenv("EVOLUTION_INSTANCE", raising=False)
    envfile.load_env_files([p])
    assert os.environ["A"] == "process"
    assert os.environ["B"] == "from-file"
    assert os.environ["GH_TOKEN"] == "tok"
    assert os.environ["ARGUS_WA_APIKEY"] == "wa-key"
    assert os.environ["ARGUS_WA_URL"] == "http://127.0.0.1:8080"
    assert os.environ["ARGUS_WA_INSTANCE"] == "hq"


def test_load_from_environment_reads_single_and_multi_files(tmp_path, monkeypatch):
    a = tmp_path / "a.env"
    b = tmp_path / "b.env"
    a.write_text("A=1\n", encoding="utf-8")
    b.write_text("B=2\n", encoding="utf-8")
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)
    monkeypatch.setenv("ARGUS_ENV_FILE", str(a))
    monkeypatch.setenv("ARGUS_ENV_FILES", str(b))
    loaded = envfile.load_from_environment()
    assert loaded == [a, b]
    assert os.environ["A"] == "1"
    assert os.environ["B"] == "2"
