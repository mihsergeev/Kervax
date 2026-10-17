package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// Версию из метки образа и дату сборки агент отдает у всех контейнеров (до 2.35 - только у прокси),
// версии из окружения - только из imageVerKeys. Список образов спрашивается раз в полчаса, и отказ
// старого прокси агента (403) тоже помнится.
func TestProxyImageVersionAndBuildDate(t *testing.T) {
	imageList.at = time.Time{}
	lists := 0
	deny := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/containers/edge/json":
			w.Write([]byte(`{"State":{},"Config":{"Labels":{"org.opencontainers.image.version":"v2.5.5","traefik.enable":"true"}}}`))
		case "/containers/app/json":
			w.Write([]byte(`{"State":{},"Config":{"Labels":{"org.opencontainers.image.version":"1.2.3"},` +
				`"Env":["PATH=/usr/local/bin","PHP_VERSION=7.4.33","DB_PASSWORD=s3cret","NODE_VERSION=$(id)","APP_SECRET=x"]}}`))
		case "/images/json":
			lists++
			if deny {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			w.Write([]byte(`[{"Id":"sha256:old","Created":1638316800},{"Id":"sha256:app","Created":1790000000}]`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	cl := &http.Client{Transport: dockerRT{base}}

	edge := dockerContainer{Name: "edge", Image: "traefik:latest", State: "running", imageID: "sha256:old"}
	app := dockerContainer{Name: "app", Image: "myapp:1.2.3", State: "running", imageID: "sha256:app"}
	dockerInspect(cl, "edge", &edge)
	dockerInspect(cl, "app", &app)
	cs := []dockerContainer{edge, app}
	now := time.Now()
	imageBuildDates(cl, cs, now)
	if cs[0].ImgVer != "v2.5.5" || cs[0].ImgCreated != 1638316800 {
		t.Fatalf("edge proxy: %+v", cs[0])
	}
	if cs[1].ImgVer != "1.2.3" || cs[1].ImgCreated != 1790000000 || len(cs[1].Ver) != 1 || cs[1].Ver["PHP_VERSION"] != "7.4.33" {
		t.Fatalf("app: %+v", cs[1])
	}
	// пароли из окружения в отчет не попадают
	b, _ := json.Marshal(cs[1])
	if strings.Contains(string(b), "s3cret") || strings.Contains(string(b), "PATH") || strings.Contains(string(b), "id)") {
		t.Fatalf("app json: %s", b)
	}
	// второй отчет в пределах получаса - без нового запроса списка
	imageBuildDates(cl, cs, now.Add(10*time.Minute))
	if lists != 1 {
		t.Fatalf("image list asked %d times", lists)
	}
	// старый прокси отвечает 403: даты нет, отказ помним до следующего окна
	deny = true
	imageBuildDates(cl, cs, now.Add(imageListTTL))
	imageBuildDates(cl, cs, now.Add(imageListTTL+time.Minute))
	if lists != 2 || cs[0].ImgCreated != 0 {
		t.Fatalf("after 403: lists=%d edge=%+v", lists, cs[0])
	}
	// без контейнеров список не нужен вовсе
	imageList.at = time.Time{}
	imageBuildDates(cl, nil, now)
	if lists != 2 {
		t.Fatalf("image list asked without containers")
	}
}
