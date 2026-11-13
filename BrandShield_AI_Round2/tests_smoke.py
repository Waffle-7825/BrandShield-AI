"""Dependency-light smoke tests for core BrandShield AI behaviour."""
import os, tempfile
os.environ["BRANDSHIELD_DATA_DIR"] = tempfile.mkdtemp(prefix="brandshield-test-")
import main


def test_password_hashing():
    saved = main.hash_password("correct-horse-battery")
    assert main.verify_password("correct-horse-battery", saved)
    assert not main.verify_password("wrong-password", saved)


def test_similarity():
    a = main.vectorize("The platform is available on web and mobile")
    b = main.vectorize("Platform available on web and mobile")
    assert main.cosine(a, b) > 0.5
    aliases=[["registered account holders", "members"]]
    c=main.vectorize("Our product is for registered account holders", aliases)
    d=main.vectorize("Our product is for members", aliases)
    assert main.cosine(c,d) > 0.5


def test_source_versioning_and_analysis():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    first = client.post("/api/sources", data={"name":"Test Guide", "content":"Avoid term: customers\nPreferred term: members\nApproved fact: The service is available on web and mobile."})
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    second = client.post("/api/sources", data={"name":"Test Guide", "content":"Avoid term: clients\nApproved fact: The service is available on web and mobile."})
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2
    versions = client.get("/api/sources/versions").json()
    assert any(x["name"] == "Test Guide" and x["version"] == 1 and x["active"] == 0 for x in versions)
    assert any(x["name"] == "Test Guide" and x["version"] == 2 and x["active"] == 1 for x in versions)
    review = client.post("/api/reviews", json={"title":"Smoke", "content":"Our clients use the service."})
    assert review.status_code == 200, review.text
    assert any(f["status"] == "CONTRADICTED" for f in review.json()["findings"])


def test_conflict_candidates():
    source_a={"id":"a","name":"Guide A","version":1}
    source_b={"id":"b","name":"Guide B","version":1}
    facts=[("The platform is available offline",source_a,1),
           ("The platform is not available offline",source_b,1)]
    assert len(main.detect_conflicts(facts)) == 1


def test_analytics_and_evaluation():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    assert client.get("/api/analytics").status_code == 200
    report = client.get("/api/evaluation")
    assert report.status_code == 200
    assert report.json()["examples"] >= 4

if __name__ == "__main__":
    for fn in (test_password_hashing, test_similarity, test_source_versioning_and_analysis, test_conflict_candidates, test_analytics_and_evaluation):
        fn(); print("PASS", fn.__name__)
