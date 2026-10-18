package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestLLMEngine(t *testing.T) {
	cases := []struct {
		cmd    string
		engine string
		port   int
		model  string
		host   string
		noMet  bool
	}{
		{"vllm serve Qwen/Qwen2.5-72B-Instruct-AWQ --tensor-parallel-size 2", "vllm", 8000, "Qwen/Qwen2.5-72B-Instruct-AWQ", "", false},
		{"/usr/bin/python3 /usr/local/bin/vllm serve /models/q --port=9001 --served-model-name qwen", "vllm", 9001, "qwen", "", false},
		{"python3 -m vllm.entrypoints.openai.api_server --model meta/llama --host 127.0.0.1 --port 8010", "vllm", 8010, "meta/llama", "127.0.0.1", false},
		{"python3 -m sglang.launch_server --model-path m/x --port 30001 --enable-metrics", "sglang", 30001, "m/x", "", false},
		{"python3 -m sglang.launch_server --model-path m/x", "sglang", 30000, "m/x", "", true},
		{"text-embeddings-router --model-id BAAI/bge-m3 --port 80", "tei", 9000, "BAAI/bge-m3", "", false},
		{"/usr/local/bin/text-embeddings-router --model-id x --prometheus-port 9100", "tei", 9100, "x", "", false},
		{"/app/.venv/bin/python3 /app/.venv/bin/litellm --config /etc/litellm/config.yaml --port 4001", "litellm", 4001, "", "", false},
		{"litellm --config /app/config.yaml", "litellm", 4000, "", "", false},
		{"python3 -m litellm --host 127.0.0.1 --model ollama/x", "litellm", 4000, "ollama/x", "127.0.0.1", false},
	}
	for _, c := range cases {
		p, ok := llmEngine(strings.Fields(c.cmd))
		if !ok || p.engine != c.engine || p.port != c.port || p.model != c.model || p.host != c.host || p.noMetrics != c.noMet {
			t.Errorf("%q: %+v %v", c.cmd, p, ok)
		}
	}
	// TEI 1.9 отдает /metrics на порту API: он запасной
	if p, _ := llmEngine(strings.Fields("text-embeddings-router --model-id x --port 8000")); !reflect.DeepEqual(p.alt, []int{8000}) {
		t.Errorf("%+v", p)
	}
	if p, _ := llmEngine(strings.Fields("text-embeddings-router --model-id x")); !reflect.DeepEqual(p.alt, []int{80, 3000}) {
		t.Errorf("%+v", p)
	}
	if p, _ := llmEngine(strings.Fields("vllm serve m --port 8000")); p.alt != nil {
		t.Errorf("%+v", p)
	}
	for _, cmd := range []string{"grep vllm.entrypoints.openai.api_server", "tail -f /var/log/vllm.log", "pip install sglang", "bash",
		"pip install litellm", "python3 /srv/litellm_sync.py", "tail -f /var/log/litellm.log"} {
		if p, ok := llmEngine(strings.Fields(cmd)); ok {
			t.Errorf("%q is not a service: %+v", cmd, p)
		}
	}
}

const fibTrie = `Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        +-- 127.0.0.0/31 1 0 0
           |-- 127.0.0.0
              /8 host LOCAL
           |-- 127.0.0.1
              /32 host LOCAL
        |-- 127.255.255.255
           /32 link BROADCAST
     +-- 172.17.0.0/16 2 0 2
        +-- 172.17.0.0/30 2 0 2
           |-- 172.17.0.0
              /16 link UNICAST
           |-- 172.17.0.2
              /32 host LOCAL
        |-- 172.17.255.255
           /32 link BROADCAST
`

func TestNetnsIP(t *testing.T) {
	d := t.TempDir()
	ioWrite(t, d+"/fib_trie", fibTrie)
	if ip := netnsIP(d + "/fib_trie"); ip != "172.17.0.2" {
		t.Fatal(ip)
	}
	ioWrite(t, d+"/lo", strings.Split(fibTrie, "     +-- 172")[0])
	if ip := netnsIP(d + "/lo"); ip != "" {
		t.Fatal("loopback only:", ip)
	}
}

func TestGPUOnPCI(t *testing.T) {
	gpuPCIState.last, gpuPCIState.list = time.Time{}, nil
	d := t.TempDir()
	dev := func(bus, vendor, class, device, drv string) {
		ioWrite(t, d+"/"+bus+"/vendor", vendor+"\n")
		ioWrite(t, d+"/"+bus+"/class", class+"\n")
		ioWrite(t, d+"/"+bus+"/device", device+"\n")
		if drv != "" {
			if err := os.Symlink("../../../bus/pci/drivers/"+drv, d+"/"+bus+"/driver"); err != nil {
				t.Fatal(err)
			}
		}
	}
	dev("0000:00:02.0", "0x8086", "0x030000", "0x46a6", "i915")
	dev("0000:41:00.0", "0x10de", "0x030200", "0x20b5", "nvidia")
	dev("0000:41:00.1", "0x10de", "0x040300", "0x1aef", "snd_hda_intel") // звук той же карты
	dev("0000:81:00.0", "0x10de", "0x030000", "0x2204", "")
	got := gpuOnPCI(d, time.Unix(1000, 0))
	want := []gpuPCI{{Bus: "0000:41:00.0", Dev: "20b5", Drv: "nvidia"}, {Bus: "0000:81:00.0", Dev: "2204"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v", got)
	}
	// раз в минуту: в пределах минуты отдается прошлый список
	os.RemoveAll(d + "/0000:81:00.0")
	if len(gpuOnPCI(d, time.Unix(1030, 0))) != 2 || len(gpuOnPCI(d, time.Unix(1061, 0))) != 1 {
		t.Fatal("cache")
	}
}

const vllmModel = `model_name="Qwen/Qwen2.5-72B-Instruct-AWQ"`

// vllmMetrics - /metrics vLLM: req - завершено запросов, ttft - накопленные бакеты TTFT
func vllmMetrics(running, waiting, kv float64, prompt, gen, stop, abort, e5 float64, ttft [4]float64) string {
	l := `engine="0",` + vllmModel
	return "# HELP vllm:num_requests_running Number of requests in model execution batches.\n" +
		"# TYPE vllm:num_requests_running gauge\n" +
		fmt.Sprintf("vllm:num_requests_running{%s} %g\n", l, running) +
		fmt.Sprintf("vllm:num_requests_waiting{%s} %g\n", l, waiting) +
		fmt.Sprintf("vllm:kv_cache_usage_perc{%s} %g\n", l, kv) +
		fmt.Sprintf("vllm:prompt_tokens_total{%s} %g\n", l, prompt) +
		fmt.Sprintf("vllm:prompt_tokens_created{%s} 1.7e+09\n", l) +
		fmt.Sprintf("vllm:generation_tokens_total{%s} %g\n", l, gen) +
		fmt.Sprintf("vllm:request_success_total{engine=\"0\",finished_reason=\"stop\",%s} %g\n", vllmModel, stop) +
		fmt.Sprintf("vllm:request_success_total{engine=\"0\",finished_reason=\"abort\",%s} %g\n", vllmModel, abort) +
		fmt.Sprintf("vllm:time_to_first_token_seconds_bucket{engine=\"0\",le=\"0.1\",%s} %g\n", vllmModel, ttft[0]) +
		fmt.Sprintf("vllm:time_to_first_token_seconds_bucket{engine=\"0\",le=\"0.5\",%s} %g\n", vllmModel, ttft[1]) +
		fmt.Sprintf("vllm:time_to_first_token_seconds_bucket{engine=\"0\",le=\"1.0\",%s} %g\n", vllmModel, ttft[2]) +
		fmt.Sprintf("vllm:time_to_first_token_seconds_bucket{engine=\"0\",le=\"+Inf\",%s} %g\n", vllmModel, ttft[3]) +
		fmt.Sprintf("vllm:time_to_first_token_seconds_count{%s} %g\n", l, ttft[3]) +
		fmt.Sprintf("http_requests_total{handler=\"/v1/chat/completions\",method=\"POST\",status=\"2xx\"} %g\n", stop) +
		fmt.Sprintf("http_requests_total{handler=\"/v1/chat/completions\",method=\"POST\",status=\"5xx\"} %g\n", e5) +
		"python_gc_objects_collected_total{generation=\"0\"} 12345.0\n"
}

func TestHistQuantiles(t *testing.T) {
	m0 := promParse([]byte(vllmMetrics(0, 0, 0, 0, 0, 0, 0, 0, [4]float64{100, 600, 900, 1000})), "vllm:")
	m1 := promParse([]byte(vllmMetrics(0, 0, 0, 0, 0, 0, 0, 0, [4]float64{120, 700, 1012, 1120})), "vllm:")
	a, b := m0.hist("vllm:time_to_first_token_seconds"), m1.hist("vllm:time_to_first_token_seconds")
	// прирост 20, 100, 112, 120: медиана внутри бакета 0.1-0.5, p95 и p99 выше 1.0 - сама граница
	if q := histQuantiles(a, b); !reflect.DeepEqual(q, []float64{0.3, 1, 1}) {
		t.Fatal(q)
	}
	if histQuantiles(a, a) != nil || histQuantiles(b, a) != nil || histQuantiles(nil, b) != nil {
		t.Fatal("no requests or reset must give nil")
	}
}

func TestLLMCompute(t *testing.T) {
	t0 := time.Unix(1_800_000_000, 0)
	inf := llmInfo{Engine: "vllm", Model: "/models/q"}
	h := llmCompute(&inf, []byte(vllmMetrics(10, 2, 0.5, 100000, 50000, 990, 10, 3, [4]float64{100, 600, 900, 1000})), nil, t0)
	if !inf.OK || *inf.Running != 10 || *inf.Waiting != 2 || *inf.KV != 50 || inf.Win != 0 || inf.ReqS != nil {
		t.Fatalf("%+v", inf)
	}
	if inf.Model != "Qwen/Qwen2.5-72B-Instruct-AWQ" {
		t.Fatal(inf.Model)
	}
	inf = llmInfo{Engine: "vllm"}
	h = llmCompute(&inf, []byte(vllmMetrics(12, 0, 0.62, 160000, 80000, 1100, 20, 5, [4]float64{120, 700, 1012, 1120})), h, t0.Add(time.Minute))
	if inf.Win != 60 || *inf.ReqS != 2 || *inf.PromptTPS != 1000 || *inf.GenTPS != 500 || *inf.Errors != 2 || *inf.Aborted != 10 {
		t.Fatalf("%+v", inf)
	}
	if !reflect.DeepEqual(inf.TTFT, []float64{0.3, 1, 1}) || inf.TPOT != nil || inf.Preempt != nil {
		t.Fatalf("%+v", inf)
	}
	// окно не длиннее llmWindow: старые замеры уходят
	for i := 2; i <= 12; i++ {
		inf = llmInfo{Engine: "vllm"}
		h = llmCompute(&inf, []byte(vllmMetrics(12, 0, 0.62, 160000, 80000, 1100, 20, 5, [4]float64{120, 700, 1012, 1120})), h, t0.Add(time.Duration(i)*time.Minute))
	}
	if inf.Win != 300 || *inf.ReqS != 0 || inf.TTFT != nil {
		t.Fatalf("%+v", inf)
	}
	// перезапуск: счетчики меньше - окно заново
	inf = llmInfo{Engine: "vllm"}
	h = llmCompute(&inf, []byte(vllmMetrics(1, 0, 0.01, 10, 5, 1, 0, 0, [4]float64{1, 1, 1, 1})), h, t0.Add(13*time.Minute))
	if len(h) != 1 || inf.Win != 0 {
		t.Fatalf("%d %+v", len(h), inf)
	}
}

func TestLLMReadTEIAndSGLang(t *testing.T) {
	tei := "te_queue_size 3\nte_request_success{method=\"single\"} 40\nte_request_success{method=\"batch\"} 2\n" +
		"te_request_failure{err=\"overloaded\"} 1\nte_request_input_length_sum 9000\n" +
		"te_request_duration_bucket{le=\"0.01\"} 30\nte_request_duration_bucket{le=\"+Inf\"} 42\n"
	g, s := llmRead("tei", promParse([]byte(tei), "te_"))
	if *g.waiting != 3 || g.running != nil || s.ctr["req"] != 42 || s.ctr["err"] != 1 || s.ctr["prompt"] != 9000 || s.hist["e2e"] == nil {
		t.Fatalf("%+v %+v", g, s)
	}
	sg := "sglang:num_running_reqs{model_name=\"m/x\"} 7\nsglang:num_queue_reqs{model_name=\"m/x\"} 1\n" +
		"sglang:token_usage{model_name=\"m/x\"} 0.93\nsglang:generation_tokens_total{model_name=\"m/x\"} 500\n"
	g, s = llmRead("sglang", promParse([]byte(sg), "sglang:"))
	if *g.running != 7 || *g.waiting != 1 || *g.kv != 93 || g.model != "m/x" || s.ctr["gen"] != 500 {
		t.Fatalf("%+v %+v", g, s)
	}
}

func TestCollectLLM(t *testing.T) {
	llmState.Lock()
	llmState.last, llmState.out, llmState.snaps = time.Time{}, nil, map[string][]llmSnap{}
	llmState.Unlock()
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/metrics" {
			http.NotFound(w, r)
			return
		}
		if n.Add(1) == 1 {
			fmt.Fprint(w, vllmMetrics(10, 2, 0.5, 100000, 50000, 990, 10, 3, [4]float64{100, 600, 900, 1000}))
			return
		}
		fmt.Fprint(w, vllmMetrics(12, 0, 0.62, 160000, 80000, 1100, 20, 5, [4]float64{120, 700, 1012, 1120}))
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	port := u.Port()

	proc := t.TempDir()
	ioWrite(t, proc+"/self/net/dev", netDev([3]string{"eth0", "1", "1"}))
	stat := "S" + strings.Repeat(" 0", 18) + " 1000 0 0"
	for _, pid := range []string{"4242", "4243"} { // сервис и его воркер с той же командной строкой
		ioWrite(t, proc+"/"+pid+"/cmdline", "python3\x00-m\x00vllm.entrypoints.openai.api_server\x00--model\x00/models/q\x00--port\x00"+port+"\x00")
		ioWrite(t, proc+"/"+pid+"/stat", pid+" (python3) "+stat)
		ioWrite(t, proc+"/"+pid+"/comm", "python3\n")
		if err := os.Symlink("../self/net", proc+"/"+pid+"/net"); err != nil {
			t.Fatal(err)
		}
	}
	// SGLang без --enable-metrics: метрик нет, стучаться некуда
	ioWrite(t, proc+"/5000/cmdline", "python3\x00-m\x00sglang.launch_server\x00--model-path\x00m/x\x00")
	ioWrite(t, proc+"/5000/comm", "python3\n")
	ioWrite(t, proc+"/77/cmdline", "bash\x00-c\x00sleep 1\x00")

	oldCg, oldUp := procCgroup, procUptime
	procCgroup = func(pid int) string { return "" }
	procUptime = func() float64 { return 1010 }
	defer func() { procCgroup, procUptime = oldCg, oldUp }()

	t0 := time.Unix(1_800_000_000, 0)
	out := collectLLM(proc, nil, nil, t0)
	if len(out) != 2 {
		t.Fatalf("%+v", out)
	}
	var v, sg llmInfo
	for _, x := range out {
		if x.Engine == "vllm" {
			v = x
		} else {
			sg = x
		}
	}
	if !v.OK || v.Err != "" || v.Name != "python3" || v.Up != 1000 || *v.Running != 10 || v.Port == 0 {
		t.Fatalf("%+v", v)
	}
	if sg.Engine != "sglang" || !sg.NoMetrics || sg.OK {
		t.Fatalf("%+v", sg)
	}
	if got := collectLLM(proc, nil, nil, t0.Add(10*time.Second)); n.Load() != 1 || len(got) != 2 {
		t.Fatal("must reuse the last scrape within llmEvery")
	}
	for _, x := range collectLLM(proc, nil, nil, t0.Add(time.Minute)) {
		if x.Engine == "vllm" && (x.Win != 60 || *x.GenTPS != 500 || x.TTFT == nil) {
			t.Fatalf("%+v", x)
		}
	}
	srv.Close()
	for _, x := range collectLLM(proc, nil, nil, t0.Add(2*time.Minute)) {
		if x.Engine == "vllm" && (x.OK || x.Err != "refused") {
			t.Fatalf("%+v", x)
		}
	}
}

// TEI 1.9 не открывает порт Prometheus: метрики берутся с порта API, в отчете тот порт, что ответил
func TestCollectLLMTEIMetricsOnAPIPort(t *testing.T) {
	llmState.Lock()
	llmState.last, llmState.out, llmState.snaps = time.Time{}, nil, map[string][]llmSnap{}
	llmState.Unlock()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "te_queue_size 2\nte_request_success{method=\"single\"} 7\n")
	}))
	defer api.Close()
	closed := httptest.NewServer(http.NotFoundHandler())
	cu, _ := url.Parse(closed.URL)
	closed.Close() // порт Prometheus никто не слушает
	au, _ := url.Parse(api.URL)

	proc := t.TempDir()
	ioWrite(t, proc+"/self/net/dev", netDev([3]string{"eth0", "1", "1"}))
	ioWrite(t, proc+"/31/cmdline", "text-embeddings-router\x00--model-id\x00BAAI/bge-m3\x00--prometheus-port\x00"+cu.Port()+"\x00--port\x00"+au.Port()+"\x00")
	ioWrite(t, proc+"/31/stat", "31 (text-embeddings) S"+strings.Repeat(" 0", 18)+" 1000 0 0")
	if err := os.Symlink("../self/net", proc+"/31/net"); err != nil {
		t.Fatal(err)
	}
	oldCg, oldUp := procCgroup, procUptime
	procCgroup = func(pid int) string { return "" }
	procUptime = func() float64 { return 1010 }
	defer func() { procCgroup, procUptime = oldCg, oldUp }()

	out := collectLLM(proc, nil, nil, time.Unix(1_800_000_000, 0))
	if len(out) != 1 || !out[0].OK || out[0].Err != "" || out[0].Port != atoiT(t, au.Port()) || *out[0].Waiting != 2 {
		t.Fatalf("%+v", out)
	}
}

func atoiT(t *testing.T, s string) int {
	n := 0
	if _, err := fmt.Sscan(s, &n); err != nil {
		t.Fatal(err)
	}
	return n
}

// LiteLLM: в работе без своего запроса к /metrics, ошибки - только ответы 5xx, задержек нет
func TestLLMReadLiteLLM(t *testing.T) {
	body := "# HELP litellm_in_flight_requests x\nlitellm_in_flight_requests 3.0\n" +
		"litellm_proxy_total_requests_metric_total{requested_model=\"a\",route=\"/v1/chat/completions\",status_code=\"200\"} 90\n" +
		"litellm_proxy_total_requests_metric_total{requested_model=\"a\",route=\"/v1/chat/completions\",status_code=\"400\"} 4\n" +
		"litellm_proxy_total_requests_metric_total{requested_model=\"b\",route=\"/v1/embeddings\",status_code=\"503\"} 2\n" +
		"litellm_proxy_total_requests_metric_total{requested_model=\"b\",route=\"/v1/embeddings\",status_code=\"500\"} 1\n" +
		"litellm_proxy_total_requests_metric_created{requested_model=\"b\"} 1.7e9\n" +
		"litellm_input_tokens_metric_total{requested_model=\"a\"} 1000\nlitellm_output_tokens_metric_total{requested_model=\"a\"} 50\n" +
		"litellm_llm_api_latency_metric_bucket{le=\"0.5\"} 3\nlitellm_llm_api_latency_metric_bucket{le=\"+Inf\"} 9\n"
	inf := llmInfo{Engine: "litellm"}
	llmCompute(&inf, []byte(body), nil, time.Unix(1_800_000_000, 0))
	if !inf.OK || inf.Running == nil || *inf.Running != 2 {
		t.Fatalf("%+v", inf)
	}
	g, s := llmRead("litellm", promParse([]byte(body), "litellm_in_flight_requests", "litellm_proxy_total_requests_metric_total",
		"litellm_input_tokens_metric_total", "litellm_output_tokens_metric_total"))
	if s.ctr["req"] != 97 || s.ctr["err"] != 3 || s.ctr["prompt"] != 1000 || s.ctr["gen"] != 50 || len(s.hist) != 0 || g.waiting != nil {
		t.Fatalf("%+v %+v", g, s)
	}
	// простой: в работе только сам запрос метрик
	g, _ = llmRead("litellm", promParse([]byte("litellm_in_flight_requests 1\n"), "litellm_in_flight_requests"))
	if *g.running != 0 {
		t.Fatal(*g.running)
	}
}

// LiteLLM в поде: стучимся сразу на /metrics/, без редиректа
func TestCollectLLMLiteLLMPath(t *testing.T) {
	llmState.Lock()
	llmState.last, llmState.out, llmState.snaps = time.Time{}, nil, map[string][]llmSnap{}
	llmState.Unlock()
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.URL.Path != "/metrics/" {
			http.Redirect(w, r, "/metrics/", http.StatusTemporaryRedirect)
			return
		}
		fmt.Fprint(w, "litellm_in_flight_requests 1\n")
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	proc := t.TempDir()
	ioWrite(t, proc+"/self/net/dev", netDev([3]string{"eth0", "1", "1"}))
	ioWrite(t, proc+"/40/cmdline", "/app/.venv/bin/python3\x00/app/.venv/bin/litellm\x00--config\x00/etc/litellm/config.yaml\x00--port\x00"+u.Port()+"\x00")
	ioWrite(t, proc+"/40/stat", "40 (python3) S"+strings.Repeat(" 0", 18)+" 1000 0 0")
	if err := os.Symlink("../self/net", proc+"/40/net"); err != nil {
		t.Fatal(err)
	}
	oldCg, oldUp := procCgroup, procUptime
	procCgroup = func(pid int) string { return "" }
	procUptime = func() float64 { return 1010 }
	defer func() { procCgroup, procUptime = oldCg, oldUp }()
	out := collectLLM(proc, nil, nil, time.Unix(1_800_000_000, 0))
	if len(out) != 1 || out[0].Engine != "litellm" || !out[0].OK || !reflect.DeepEqual(paths, []string{"/metrics/"}) {
		t.Fatalf("%+v %v", out, paths)
	}
}
