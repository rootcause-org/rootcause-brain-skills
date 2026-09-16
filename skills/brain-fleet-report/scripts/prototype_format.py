"""One markdown/decision contract for report publication and open-card repairs."""
import re
from fr_common import reduce_names, strip_emails

MARKER = "\n\n<!-- helper-prototype -->\n"


def reduced(text):
    text = re.sub(r"(?i)\b(?:postgres(?:ql)?)://[^\s<>]+", "[URL removed]", str(text))
    text = re.sub(r"(?i)(password|secret|token|api[_-]?key|authorization)([\s\"':=]+)[^\s,}]+", r"\1\2[redacted]", text)
    text = reduce_names(strip_emails(text))
    return text if len(text) <= 5800 else text[:5800] + "\n[trimmed]"


def fenced(text):
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))
    return fence + "text\n" + text + "\n" + fence


def prototype_markdown(p):
    parts = [f"Branch: {p.repo}@{p.branch} ({p.files} files)",
             "Command:\n\n" + fenced(reduced(p.command))]
    for label, result in (("Before", p.before), ("After", p.after)):
        parts += [f"## {label}", f"Commit: {result.sha} · console run: {result.run_id} · step: {result.seq}",
                  fenced(f"exit: {result.exit_code}\nstdout:\n{reduced(result.stdout)}\nstderr:\n{reduced(result.stderr)}")]
    return "\n\n".join(parts)


def merge_option(p):
    return {"label": "Merge branch", "instruction":
            f"git merge {p.branch} from {p.repo}, run tests, publish/release as the repo requires. "
            f"Verified prototype commit: {p.after.sha}; recheck if the branch advanced."}
