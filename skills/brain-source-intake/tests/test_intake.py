"""End to end checks for the source-intake engine.

    cd skills/brain-source-intake && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from html import escape
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FIXTURES = SKILL / "fixtures"


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          text=True, capture_output=True, check=False)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------- mini repo

def mini_repo(root: Path) -> None:
    """Two apps in one checkout: a CodeIgniter legacy app and a Laravel app."""
    write(root / "legacy/index.php", "<?php require 'application/bootstrap.php';\n")
    write(root / "legacy/application/config/config.php", "<?php $config = [];\n")
    write(root / "legacy/application/config/routes.php", "<?php $route['default'] = 'home';\n")
    write(root / "legacy/application/models/Salon_model.php",
          "<?php class Salon_model { protected $table = 'users'; }\n")
    write(root / "legacy/application/controllers/cron/Reminders.php", "<?php class Reminders {}\n")
    write(root / "app/composer.json", json.dumps(
        {"require": {"laravel/framework": "^10.0", "mollie/mollie-api-php": "^2.0"}}) + "\n")
    write(root / "app/artisan", "#!/usr/bin/env php\n")
    write(root / "app/routes/web.php", "<?php Route::get('/', fn () => view('home'));\n")
    write(root / "app/app/Models/Treatment.php",
          "<?php class Treatment { protected $table = 'treatments'; }\n")
    write(root / "app/app/Mail/ReminderMail.php", "<?php class ReminderMail {}\n")
    write(root / "app/app/Console/Kernel.php", "<?php class Kernel {}\n")
    write(root / "app/database/migrations/2020_01_01_create_treatments.php",
          "<?php // create treatments\n")
    write(root / "app/.env.example", "APP_KEY=\n")
    write(root / "app/.env", "APP_KEY=base64:supersecretvalue\n")
    write(root / "app/vendor/foo/bar.php", "<?php // vendored\n")


def tables_file(tmp: Path) -> Path:
    path = tmp / "tables.txt"
    path.write_text("# the tables the dev named\nusers\ntreatments\nghost\n", encoding="utf-8")
    return path


def app_named(scan: dict, suffix: str) -> dict:
    return next(app for app in scan["repos"][0]["apps"] if app["root"].endswith(suffix))


def framework_names(app: dict) -> set[str]:
    return {f["name"] for f in app["frameworks"]}


def test_scan_local(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    mini_repo(repo)
    out = tmp_path / "out"
    done = run("scan.py", "--repo", f"app={repo}", "--tables", str(tables_file(tmp_path)),
               "--out", str(out))
    assert done.returncode == 0, done.stderr

    scan = json.loads((out / "scan.json").read_text())
    assert scan["repos"][0]["mode"] == "local"
    assert len(scan["repos"][0]["apps"]) == 2
    assert "laravel" in framework_names(app_named(scan, "/app"))
    assert "codeigniter" in framework_names(app_named(scan, "/legacy"))

    listing = (out / "raw" / "listing-app.txt").read_text()
    assert "app/.env.example" in listing
    assert "app/.env\n" not in listing
    assert "vendor/" not in listing
    assert ".env" not in (out / "scan.md").read_text().replace(".env.example", "")

    mollie = next(hit for hit in app_named(scan, "/app")["integrations"]
                  if hit["vendor"] == "mollie")
    assert mollie["dep"] is True

    rows = {line.split("\t")[0]: line.split("\t")
            for line in (out / "tables.tsv").read_text().splitlines()[1:]
            if not line.startswith("#")}
    assert "Salon_model.php" in rows["users"][1]
    assert "Treatment.php" in rows["treatments"][1]
    assert "ghost" in rows and rows["ghost"][1] == ""
    assert "# unmatched (1)" in (out / "tables.tsv").read_text()


def test_scan_listing(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    mini_repo(repo)
    rels = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file())
    rels = [rel for rel in rels if "/vendor/" not in rel and not rel.endswith("/.env")]
    listing = tmp_path / "find.txt"
    listing.write_text("\n".join(f"/mirrors/app/{rel}" for rel in rels) + "\n", encoding="utf-8")

    out = tmp_path / "out"
    done = run("scan.py", "--listing", f"app={listing}", "--tables", str(tables_file(tmp_path)),
               "--out", str(out))
    assert done.returncode == 0, done.stderr

    scan = json.loads((out / "scan.json").read_text())
    repo_record = scan["repos"][0]
    assert repo_record["mode"] == "listing"
    assert repo_record["root"] == "/mirrors/app"
    assert "mode: listing (contents not read)" in (out / "scan.md").read_text()
    assert "laravel" in framework_names(app_named(scan, "/app"))
    assert "codeigniter" in framework_names(app_named(scan, "/legacy"))
    # no contents were read, so no line counts, no deps and no declared tables
    assert all(item["lines"] == 0
               for item in app_named(scan, "/app")["areas"]["entry"])
    assert app_named(scan, "/app")["deps"] == []
    assert all(not row["model_files"] for row in scan["tables"])
    assert next(row for row in scan["tables"] if row["table"] == "treatments")["guess"].endswith(
        "app/app/Models/Treatment.php")


# -------------------------------------------------------------------- fixture

def copy_fixture(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    shutil.copytree(FIXTURES / "out", out)
    return out


def test_validate_and_render_fixture(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    done = run("validate.py", str(out))
    assert done.returncode == 0, done.stderr
    assert "OK: 8 questions" in done.stdout

    data = json.loads((out / "intake.json").read_text())
    assert data["project"] == "salonx"
    assert data["tally"] == {"found": 3, "ambiguous": 2, "missing": 2, "n_a": 1}
    assert set(data["proposal"]) == {"INDEX.md", "booking.md", "mail.md"}

    done = run("render.py", str(out / "intake.json"))
    assert done.returncode == 0, done.stderr
    html = (out / "report.html").read_text()
    for question in data["devquestions"]:
        assert escape(question["question"], quote=True) in html
    assert "Copy all as Markdown" in html
    assert 'type="radio"' in html
    assert "<link" not in html
    assert "<script src" not in html
    # every external-looking URL lives inside the escaped data, never in a src/href attribute
    assert 'src="http' not in html and 'href="http' not in html


def test_render_from_sample_intake_json(tmp_path: Path) -> None:
    target = tmp_path / "intake.json"
    shutil.copy(FIXTURES / "intake.json", target)
    done = run("render.py", str(target))
    assert done.returncode == 0, done.stderr
    html = (tmp_path / "report.html").read_text()
    assert "Source intake" in html
    assert "Where each question would be grounded (benchmark)" in html


def test_validate_errors(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)

    benchmark = (out / "benchmark.tsv").read_text().replace(
        "/mirrors/app/app/app/Models/Booking.php:isOnlineBookable",
        "/mirrors/app/app/app/Models/Ghost.php")
    (out / "benchmark.tsv").write_text(benchmark, encoding="utf-8")

    rows = [line for line in (out / "devquestions.tsv").read_text().splitlines()
            if not line.startswith("D6")]
    rows.append("D7\tconventions\tIs there anything else we should know?\t\t")
    (out / "devquestions.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    mail = (out / "proposal" / "mail.md").read_text()
    mail += "\nThe legacy library is dead code, we think \u2014 nobody could confirm it.\n"
    mail += "\n```php\nMail::to($patient)->send(new ReminderMail());\n```\n"
    (out / "proposal" / "mail.md").write_text(mail, encoding="utf-8")

    done = run("validate.py", str(out))
    assert done.returncode == 1
    for expected in (
        "benchmark.tsv: Q1:",
        "is not in raw/listing-app.txt",
        "devquestions.tsv: cluster 'exports'",
        "devquestions.tsv: D7: empty evidence",
        "proposal/mail.md",
        "fenced code block",
        "em dash",
    ):
        assert expected in done.stderr, done.stderr
    assert not (out / "intake.json").exists()


# ------------------------------------------------------------------ questions

def test_questions_from_line_list(tmp_path: Path) -> None:
    source = tmp_path / "asked.txt"
    source.write_text("# what the owner remembers\n"
                      "2026-08-02 Waarom ziet mijn klant geen vrije slots?\n"
                      "\n"
                      "Kan ik de herinnering later laten vertrekken?\n", encoding="utf-8")
    out = tmp_path / "out"
    done = run("questions.py", "--from", str(source), "--out", str(out))
    assert done.returncode == 0, done.stderr
    rows = (out / "questions.tsv").read_text().splitlines()
    assert rows[0] == "id\tdate\tchannel\tquestion\turl"
    assert rows[1].split("\t")[:3] == ["Q1", "2026-08-02", "manual"]
    assert rows[2].split("\t")[1] == ""
    assert len(rows) == 3


def test_questions_from_evidence_json(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps({"conversations": [
        {"id": "C2", "created_at": "2026-08-09T10:00:00Z", "channel": "chat",
         "first_message": "Waar   vind ik de factuur?", "url": "https://example.test/2"},
        {"id": "C1", "created_at": "2026-08-01T10:00:00Z", "channel": "email",
         "subject": "reminder", "first_raw": "De herinnering kwam niet aan."},
        {"id": "C3", "created_at": "2026-08-11T10:00:00Z", "channel": "email",
         "first_message": "out of office", "noise": True},
    ]}), encoding="utf-8")
    out = tmp_path / "out"
    done = run("questions.py", "--from", str(source), "--out", str(out))
    assert done.returncode == 0, done.stderr
    rows = [line.split("\t") for line in (out / "questions.tsv").read_text().splitlines()[1:]]
    assert [row[0] for row in rows] == ["Q1", "Q2"]
    assert rows[0][3] == "De herinnering kwam niet aan."
    assert rows[1][3] == "Waar vind ik de factuur?"
    assert rows[1][4] == "https://example.test/2"
