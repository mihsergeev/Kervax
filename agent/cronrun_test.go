package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func cronJobs(t *testing.T, raw string) []kubeJobItem {
	t.Helper()
	var d struct{ Items []kubeJobItem }
	if err := json.Unmarshal([]byte(raw), &d); err != nil {
		t.Fatal(err)
	}
	return d.Items
}

const cronJobsJSON = `{"items":[
 {"metadata":{"name":"nightly-29872800","namespace":"default","creationTimestamp":"2026-10-19T00:00:00Z",
   "ownerReferences":[{"kind":"CronJob","name":"nightly"}]},
  "status":{"startTime":"2026-10-19T00:00:00Z","completionTime":"2026-10-19T05:39:07Z","succeeded":1,
   "conditions":[{"type":"Complete","status":"True","lastTransitionTime":"2026-10-19T05:39:07Z"}]}},
 {"metadata":{"name":"nightly-29874240","namespace":"default","creationTimestamp":"2026-10-20T00:00:00Z",
   "ownerReferences":[{"kind":"CronJob","name":"nightly"}]},
  "status":{"startTime":"2026-10-20T00:00:00Z","failed":7,
   "conditions":[{"type":"Failed","status":"True","reason":"BackoffLimitExceeded","message":"Job has reached the specified backoff limit","lastTransitionTime":"2026-10-20T00:20:00Z"}]}}
]}`

func TestCronLastJob(t *testing.T) {
	jobs := cronJobs(t, cronJobsJSON)
	// последний запуск по расписанию - 20.10 00:00 UTC, его Job есть
	j, want, ok := cronLastJob("nightly", 1792454400, jobs)
	if !ok || j == nil || j.Metadata.Name != "nightly-29874240" || want != "nightly-29874240" {
		t.Fatalf("got %v %q %v", j, want, ok)
	}
	r := cronRunOf(*j)
	if r.St != "failed" || r.Reason != "BackoffLimitExceeded" || r.Done != 1792455600 {
		t.Fatalf("run %+v", r)
	}
	// запуск 21.10 был, а его Job уже удален по истории
	if j, want, ok := cronLastJob("nightly", 1792540800, jobs); ok || j != nil || want != "nightly-29875680" {
		t.Fatalf("gone: %v %q %v", j, want, ok)
	}
	// успешный прогон
	if r := cronRunOf(jobs[0]); r.St != "ok" || r.Done != 1792388347 {
		t.Fatalf("ok run %+v", r)
	}
	// не запускался ни разу, Job нет
	if j, want, ok := cronLastJob("nightly", 0, nil); ok || j != nil || want != "" {
		t.Fatalf("never: %v %q %v", j, want, ok)
	}
}

func TestCronPodCause(t *testing.T) {
	var pods []kubeJobPod
	raw := `[
	 {"metadata":{"name":"nightly-29874240-aaaaa","creationTimestamp":"2026-10-20T00:00:00Z"},
	  "status":{"containerStatuses":[{"name":"app","state":{"terminated":{"reason":"Error","exitCode":1}}}]}},
	 {"metadata":{"name":"nightly-29874240-bbbbb","creationTimestamp":"2026-10-20T00:10:00Z"},
	  "status":{"containerStatuses":[{"name":"app","state":{"terminated":{"reason":"OOMKilled","exitCode":137}}}]}}]`
	if err := json.Unmarshal([]byte(raw), &pods); err != nil {
		t.Fatal(err)
	}
	var r kubeCronRun
	if c := cronPodCause(pods, &r); c != "app" || r.Pod != "nightly-29874240-bbbbb" || r.PR != "OOMKilled" || r.X == nil || *r.X != 137 {
		t.Fatalf("oom: %q %+v", c, r)
	}
	// образ не скачался: контейнер не запускался, лога нет
	raw = `[{"metadata":{"name":"p-ccccc","creationTimestamp":"2026-10-20T00:00:00Z"},
	  "status":{"containerStatuses":[{"name":"app","state":{"waiting":{"reason":"ImagePullBackOff"}}}]}}]`
	pods = nil
	_ = json.Unmarshal([]byte(raw), &pods)
	r = kubeCronRun{}
	if c := cronPodCause(pods, &r); c != "" || r.PR != "ImagePullBackOff" || r.X != nil {
		t.Fatalf("pull: %q %+v", c, r)
	}
	// выселен: причина пода главнее Error контейнера
	raw = `[{"metadata":{"name":"p-ddddd","creationTimestamp":"2026-10-20T00:00:00Z"},
	  "status":{"reason":"Evicted","containerStatuses":[{"name":"app","state":{"terminated":{"reason":"Error","exitCode":137}}}]}}]`
	pods = nil
	_ = json.Unmarshal([]byte(raw), &pods)
	r = kubeCronRun{}
	cronPodCause(pods, &r)
	if r.PR != "Evicted" || r.X == nil || *r.X != 137 {
		t.Fatalf("evicted: %+v", r)
	}
}

func TestCronEventsOf(t *testing.T) {
	var items []kubeCronEvent
	raw := `[
	 {"type":"Normal","reason":"SuccessfulCreate","message":"Created job nightly-1","lastTimestamp":"2026-10-20T00:00:00Z","involvedObject":{"kind":"CronJob","namespace":"default","name":"nightly"}},
	 {"type":"Warning","reason":"FailedCreate","message":"Error creating job: jobs.batch is forbidden: exceeded quota","lastTimestamp":"2026-10-21T00:00:00Z","involvedObject":{"kind":"CronJob","namespace":"default","name":"nightly"}},
	 {"type":"Normal","reason":"JobAlreadyActive","message":"Not starting job because prior execution is running and concurrency policy is Forbid","eventTime":"2026-10-22T00:00:00.000001Z","involvedObject":{"kind":"CronJob","namespace":"default","name":"hourly"}},
	 {"type":"Normal","reason":"SawCompletedJob","message":"Saw completed job: nightly-29874240, condition: Failed","lastTimestamp":"2026-10-20T00:20:00Z","involvedObject":{"kind":"CronJob","namespace":"default","name":"nightly"}},
	 {"type":"Warning","reason":"BackOff","message":"x","lastTimestamp":"2026-10-22T00:00:00Z","involvedObject":{"kind":"Pod","namespace":"default","name":"p"}}]`
	if err := json.Unmarshal([]byte(raw), &items); err != nil {
		t.Fatal(err)
	}
	evs, done := cronEventsOf(items)
	if e := evs["default/nightly"]; e.R != "FailedCreate" || !e.W || e.T != 1792540800 {
		t.Fatalf("nightly ev %+v", e)
	}
	if e := evs["default/hourly"]; e.R != "JobAlreadyActive" || e.W || e.T != 1792627200 {
		t.Fatalf("hourly ev %+v", e)
	}
	if d := done["default/nightly"]; d.Job != "nightly-29874240" || d.Cond != "Failed" {
		t.Fatalf("done %+v", d)
	}
	if len(evs) != 2 {
		t.Fatalf("pod event leaked: %+v", evs)
	}
}

func TestScrubSecrets(t *testing.T) {
	cases := map[string]string{
		`connect failed: postgres://app:fakepass@db:5432/app`:      `connect failed: postgres://app:***@db:5432/app`,
		`auth error, password=hunter2 user=app`:                    `auth error, password=*** user=app`,
		`"api_key": "abc123", next`:                                `"api_key": "***", next`,
		`Authorization: Bearer fake-token-example`:                 `Authorization: Bearer ***`,
		`token 0123456789abcdef0123456789abcdef0123 rejected`:      `token *** rejected`,
		`SQLSTATE[HY000] [2002] Connection refused`:                `SQLSTATE[HY000] [2002] Connection refused`,
		`open /var/lib/app/storage/cache/very-long-file-name.json`: `open /var/lib/app/storage/cache/very-long-file-name.json`,
	}
	for in, want := range cases {
		if got := scrubSecrets(in); got != want {
			t.Errorf("scrub(%q) = %q, want %q", in, got, want)
		}
	}
	if strings.Contains(scrubSecrets("x PASSWD: 'qwe rty'"), "qwe") {
		t.Errorf("quoted password leaked")
	}
}
