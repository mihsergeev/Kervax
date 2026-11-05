package main

import "testing"

func TestVectorDatabaseImagesSentForKubernetesAudit(t *testing.T) {
	for _, tc := range []struct {
		image string
		want  bool
	}{
		{"qdrant/qdrant:v1.15", true},
		{"milvusdb/milvus:v2.6", true},
		{"cr.weaviate.io/semitechnologies/weaviate:1.40", true},
		{"chromadb/chroma:1.0", true},
		{"ghcr.io/chroma-core/chroma@sha256:abcdef", true},
		{"registry.example:5000/chromadb/chroma:1.0", true},
		{"qdrant/qdrant-client:latest", false},
		{"registry/qdrant-operator/qdrant:latest", false},
		{"zilliztech/milvus-backup:latest", false},
		{"semitechnologies/weaviate-console:latest", false},
		{"chroma/chroma-exporter:latest", false},
		{"myapp/chroma:latest", false},
		{"notchromadb/chroma:latest", false},
	} {
		if got := dbImageLike(tc.image); got != tc.want {
			t.Errorf("dbImageLike(%q) = %v, want %v", tc.image, got, tc.want)
		}
	}
}

func TestVectorNativeProcessSignatures(t *testing.T) {
	for comm, engine := range map[string]string{
		"qdrant": "Qdrant", "milvus": "Milvus", "weaviate": "Weaviate",
	} {
		if got := dbProcSignatures[comm]; got != engine || notDBComm(comm) {
			t.Errorf("native %s = %q, blocked=%v", comm, got, notDBComm(comm))
		}
	}
	for _, comm := range []string{"qdrant-client", "milvus-backup", "weaviate-cli", "chroma"} {
		if got := dbProcSignatures[comm]; got != "" {
			t.Errorf("tool %s detected as %s", comm, got)
		}
	}
}
