import os
import markdown
from app.router import Router
from app.responses import jsonify
from app.api_utils import make_json_error, sliding_window_rate_limiter

legal_bp=Router("legal")

LEGAL_DOCS=("terms", "privacy", "rules")
LEGAL_DIR=os.path.join(os.getcwd(), "legal")

def legal_present():
    return {doc: os.path.isfile(os.path.join(LEGAL_DIR, f"{doc}.md")) for doc in LEGAL_DOCS}

@legal_bp.route("/legal/<string:doc>")
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def get_legal(doc):
    if doc not in LEGAL_DOCS: return make_json_error(404, "Not found")
    path=os.path.join(LEGAL_DIR, f"{doc}.md")
    if not os.path.isfile(path): return make_json_error(404, "Not found")
    with open(path, "r", encoding="utf-8") as f: content=f.read()
    return jsonify({"type": doc, "content": content, "html": markdown.markdown(content, extensions=["extra", "nl2br", "sane_lists"])})
