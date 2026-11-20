import hashlib
import re

import pytest
from langchain_core.embeddings import Embeddings
from qdrant_client import QdrantClient

from app.config import Settings
from app.store import Store


class FakeEmbeddings(Embeddings):
    """Offline bag-of-words vectors, not a substitute for model evaluation."""
    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        result = [0.0] * 64
        for word in re.findall(r"\w+", text.lower()):
            bucket = int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % 64
            result[bucket] += 1
        return result


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, _env_file=None, min_similarity=0)


@pytest.fixture
def store(settings):
    value = Store(settings, embedding=FakeEmbeddings(), client=QdrantClient(":memory:"))
    yield value
    value.close()


@pytest.fixture
def html():
    # Synthetic test data only. Never import these fixtures into a live knowledge base.
    return b"""<html><head><title>SYNTHETIC TEST FIXTURE</title></head><body>
    <nav>Ignore navigation offer</nav><main><h1>Comprehensive car insurance</h1>
    <h2>Cover</h2><p>Example cover includes accidental damage.</p>
    <p>Example exclusion: wear and tear is not covered.</p>
    <h2>Comparison</h2><table><tr><th>Feature</th><th>Example plan</th></tr>
    <tr><td>Glass</td><td>Optional, conditions apply</td></tr></table>
    <h2>Questions</h2><details><summary>Can I claim?</summary>
    <p>Example answer: contact the claims team.</p></details>
    </main><footer>Not a real Allianz policy</footer></body></html>"""
