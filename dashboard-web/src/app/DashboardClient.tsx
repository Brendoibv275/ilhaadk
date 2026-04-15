"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type Dict = Record<string, unknown>;
type StageCount = { stage: string; total: number };
type OverviewResponse = {
  stage_counts: StageCount[];
  jobs_pending: number;
  jobs_failed: number;
  appointments_total: number;
  scheduled_leads: number;
};
type ListResponse<T> = { items: T[] };
type FinanceSummaryResponse = {
  dedicated: Record<string, string>;
  forecast: Record<string, string | number>;
};

type AppointmentRow = Record<string, unknown>;
type JobRow = Record<string, unknown>;
type FinanceRow = Record<string, unknown>;
type MessageRow = Record<string, unknown>;

type DashboardData = {
  overview: OverviewResponse;
  appointments: AppointmentRow[];
  jobs: JobRow[];
  callbacks: JobRow[];
  messages: MessageRow[];
  financeSummary: FinanceSummaryResponse;
  financeEntries: FinanceRow[];
};

type FinanceFormState = {
  entry_type: "income" | "expense";
  category: string;
  amount: string;
  due_date: string;
  employee_name: string;
  description: string;
};

const API_PREFIX = "/api/dashboard";
const FETCH_TIMEOUT_MS = 8000;
const CHART_COLORS = ["#4f46e5", "#0891b2", "#16a34a", "#f59e0b", "#dc2626", "#6d28d9"];

const DEFAULT_DATA: DashboardData = {
  overview: { stage_counts: [], jobs_pending: 0, jobs_failed: 0, appointments_total: 0, scheduled_leads: 0 },
  appointments: [],
  jobs: [],
  callbacks: [],
  messages: [],
  financeSummary: { dedicated: {}, forecast: {} },
  financeEntries: [],
};

const TABS = [
  { id: "home", label: "Visão geral" },
  { id: "leads", label: "Leads" },
  { id: "atendimento", label: "Operação" },
  { id: "agenda", label: "Agenda" },
  { id: "financeiro", label: "Financeiro" },
] as const;

type TabId = (typeof TABS)[number]["id"];

async function getJson<T>(path: string): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_PREFIX}${path}`, { cache: "no-store", signal: controller.signal });
    if (!res.ok) throw new Error(`Erro em ${path}: ${res.status}`);
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function money(value: unknown) {
  const n = Number(value || 0);
  return new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(Number.isFinite(n) ? n : 0);
}

function dateTime(value: unknown) {
  if (!value) return "-";
  const d = new Date(String(value));
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" }).format(d);
}

function statusTone(status: string) {
  if (["failed", "cancelled", "critical"].includes(status)) return "danger";
  if (["pending", "running", "warning"].includes(status)) return "warning";
  if (["completed", "paid", "ok", "scheduled"].includes(status)) return "success";
  return "neutral";
}

function StatusBadge({ label, tone }: { label: string; tone: "neutral" | "success" | "warning" | "danger" }) {
  return <span className={`status-badge status-${tone}`}>{label}</span>;
}

function MetricCard({
  title,
  value,
  caption,
  tone = "neutral",
}: {
  title: string;
  value: string;
  caption?: string;
  tone?: "neutral" | "success" | "warning" | "danger";
}) {
  return (
    <article className={`kpi-card kpi-${tone}`}>
      <p className="kpi-title">{title}</p>
      <p className="kpi-value">{value}</p>
      {caption ? <p className="kpi-caption">{caption}</p> : null}
    </article>
  );
}

function ChartPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{title}</h3>
      </div>
      <div className="chart-box">{children}</div>
    </section>
  );
}

export default function DashboardClient() {
  const [activeTab, setActiveTab] = useState<TabId>("home");
  const [data, setData] = useState<DashboardData>(DEFAULT_DATA);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [jobStatusFilter, setJobStatusFilter] = useState("all");
  const [messageFilter, setMessageFilter] = useState("all");
  const [entryOriginFilter, setEntryOriginFilter] = useState("all");
  const [financeBusy, setFinanceBusy] = useState(false);
  const [financeFeedback, setFinanceFeedback] = useState("");
  const [financeForm, setFinanceForm] = useState<FinanceFormState>({
    entry_type: "expense",
    category: "despesa_operacional",
    amount: "",
    due_date: "",
    employee_name: "",
    description: "",
  });

  const loadCore = useCallback(async () => {
    const [overview, callbacks, jobs] = await Promise.all([
      getJson<OverviewResponse>("/overview"),
      getJson<ListResponse<JobRow>>("/callbacks"),
      getJson<ListResponse<JobRow>>("/jobs"),
    ]);
    setData((prev) => ({ ...prev, overview, callbacks: callbacks.items || [], jobs: jobs.items || [] }));
    setLastUpdated(new Date());
  }, []);

  const loadLeads = useCallback(async () => {
    const messages = await getJson<ListResponse<MessageRow>>("/messages");
    setData((prev) => ({ ...prev, messages: messages.items || [] }));
  }, []);

  const loadAgenda = useCallback(async () => {
    const appointments = await getJson<ListResponse<AppointmentRow>>("/appointments");
    setData((prev) => ({ ...prev, appointments: appointments.items || [] }));
  }, []);

  const loadFinance = useCallback(async () => {
    const [financeSummary, financeEntries] = await Promise.all([
      getJson<FinanceSummaryResponse>("/finance/summary"),
      getJson<ListResponse<FinanceRow>>("/finance/entries"),
    ]);
    setData((prev) => ({ ...prev, financeSummary, financeEntries: financeEntries.items || [] }));
  }, []);

  const loadAll = useCallback(async () => {
    setError("");
    const results = await Promise.allSettled([loadCore(), loadLeads(), loadAgenda(), loadFinance()]);
    const firstError = results.find((result) => result.status === "rejected") as PromiseRejectedResult | undefined;
    if (firstError) {
      const detail = firstError.reason instanceof Error ? firstError.reason.message : "Falha ao carregar dados";
      setError(`${detail}. Verifique BACKEND_INTERNAL_URL no front e o backend FastAPI.`);
    }
    setLoading(false);
  }, [loadAgenda, loadCore, loadFinance, loadLeads]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    const core = window.setInterval(() => loadCore().catch(() => null), 10000);
    const ops = window.setInterval(() => Promise.all([loadAgenda(), loadLeads()]).catch(() => null), 22000);
    const fin = window.setInterval(() => loadFinance().catch(() => null), 35000);
    return () => {
      window.clearInterval(core);
      window.clearInterval(ops);
      window.clearInterval(fin);
    };
  }, [loadAgenda, loadCore, loadFinance, loadLeads]);

  const dedicated = (data.financeSummary.dedicated || {}) as Dict;
  const forecast = (data.financeSummary.forecast || {}) as Dict;
  const operationalBalance = Number(dedicated.total_income || 0) - Number(dedicated.total_expense || 0);

  const healthStatus = useMemo(() => {
    if (Number(data.overview.jobs_failed) > 0) return { label: "Crítico", tone: "danger" as const };
    if (data.callbacks.length > 0 || Number(data.overview.jobs_pending) > 0) return { label: "Atenção", tone: "warning" as const };
    return { label: "Operação estável", tone: "success" as const };
  }, [data.callbacks.length, data.overview.jobs_failed, data.overview.jobs_pending]);

  const stageChartData = data.overview.stage_counts.map((stage) => ({ name: stage.stage, value: Number(stage.total || 0) }));

  const jobsByStatus = useMemo(() => {
    const map = new Map<string, number>();
    for (const job of data.jobs) {
      const status = String(job.status || "unknown");
      map.set(status, (map.get(status) || 0) + 1);
    }
    return Array.from(map.entries()).map(([status, total]) => ({ status, total }));
  }, [data.jobs]);

  const entriesFiltered = useMemo(() => {
    return data.financeEntries.filter((entry) => {
      if (entryOriginFilter === "all") return true;
      const metadata = (entry.metadata as Dict | undefined) || {};
      const source = String(metadata.source || "agente");
      return entryOriginFilter === source;
    });
  }, [data.financeEntries, entryOriginFilter]);

  const expenseByCategory = useMemo(() => {
    const map = new Map<string, number>();
    for (const entry of entriesFiltered) {
      if (String(entry.entry_type) !== "expense") continue;
      const cat = String(entry.category || "outros");
      map.set(cat, (map.get(cat) || 0) + Number(entry.amount || 0));
    }
    return Array.from(map.entries()).map(([name, value]) => ({ name, value }));
  }, [entriesFiltered]);

  const financeTrend = useMemo(() => {
    const sorted = [...entriesFiltered]
      .map((entry) => ({
        date: String(entry.created_at || ""),
        entry_type: String(entry.entry_type || "expense"),
        amount: Number(entry.amount || 0),
      }))
      .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
    const buckets = new Map<string, { income: number; expense: number }>();
    for (const item of sorted) {
      const key = item.date ? item.date.slice(0, 10) : "sem_data";
      const current = buckets.get(key) || { income: 0, expense: 0 };
      if (item.entry_type === "income") current.income += item.amount;
      else current.expense += item.amount;
      buckets.set(key, current);
    }
    return Array.from(buckets.entries())
      .map(([date, val]) => ({ date, ...val }))
      .slice(-10);
  }, [entriesFiltered]);

  const filteredJobs = useMemo(
    () => data.jobs.filter((job) => (jobStatusFilter === "all" ? true : String(job.status || "") === jobStatusFilter)),
    [data.jobs, jobStatusFilter],
  );

  const filteredMessages = useMemo(
    () => data.messages.filter((msg) => (messageFilter === "all" ? true : String(msg.role || "user") === messageFilter)),
    [data.messages, messageFilter],
  );

  async function submitFinanceEntry(event: React.FormEvent) {
    event.preventDefault();
    if (!financeForm.amount || Number(financeForm.amount) <= 0) {
      setFinanceFeedback("Informe valor maior que zero.");
      return;
    }
    setFinanceBusy(true);
    setFinanceFeedback("");
    try {
      const res = await fetch(`${API_PREFIX}/finance/entries`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entry_type: financeForm.entry_type,
          category: financeForm.category,
          amount: Number(financeForm.amount),
          due_date: financeForm.due_date || undefined,
          description: financeForm.description,
          status: "pending",
          metadata: { source: "manual", employee_name: financeForm.employee_name || undefined },
        }),
      });
      if (!res.ok) throw new Error(`Falha ao salvar (${res.status})`);
      setFinanceFeedback("Lançamento salvo e painel atualizado.");
      setFinanceForm((prev) => ({ ...prev, amount: "", due_date: "", employee_name: "", description: "" }));
      await loadFinance();
    } catch (err) {
      setFinanceFeedback(err instanceof Error ? err.message : "Erro ao salvar lançamento");
    } finally {
      setFinanceBusy(false);
    }
  }

  if (loading) {
    return <main className="dashboard-root"><div className="loading-state">Carregando dados executivos...</div></main>;
  }

  return (
    <main className="dashboard-root">
      <div className="dashboard-shell modern-shell">
        <header className="top-header executive-head">
          <div>
            <h1>Dashboard SDR Ilha Ar</h1>
            <p>Visão executiva em tempo real para operação, leads e financeiro.</p>
          </div>
          <div className="header-meta">
            <StatusBadge label={healthStatus.label} tone={healthStatus.tone} />
            <span className="last-updated">Atualizado: {lastUpdated ? dateTime(lastUpdated.toISOString()) : "-"}</span>
          </div>
        </header>

        {error ? <div className="error-banner">{error}</div> : null}

        <nav className="tab-row desktop-tabs" aria-label="Navegação principal">
          {TABS.map((tab) => (
            <button key={tab.id} type="button" className={activeTab === tab.id ? "tab-btn is-active" : "tab-btn"} onClick={() => setActiveTab(tab.id)}>
              {tab.label}
            </button>
          ))}
        </nav>

        <section className="metric-grid premium-grid">
          <MetricCard title="Receita total" value={money(dedicated.total_income)} tone="success" />
          <MetricCard title="Despesa total" value={money(dedicated.total_expense)} tone="danger" />
          <MetricCard title="Saldo operacional" value={money(operationalBalance)} tone={operationalBalance >= 0 ? "success" : "danger"} />
          <MetricCard title="Pendências" value={money(dedicated.total_pending)} tone="warning" />
        </section>

        {activeTab === "home" && (
          <section className="grid-two">
            <ChartPanel title="Funil por estágio">
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie data={stageChartData} dataKey="value" nameKey="name" innerRadius={62} outerRadius={94}>
                    {stageChartData.map((entry, idx) => (
                      <Cell key={entry.name} fill={CHART_COLORS[idx % CHART_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            </ChartPanel>
            <ChartPanel title="Jobs por status">
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={jobsByStatus}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="status" />
                  <YAxis />
                  <Tooltip />
                  <Bar dataKey="total" fill="#4f46e5" radius={[8, 8, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartPanel>
            <section className="panel full-width">
              <div className="panel-head"><h3>Ações prioritárias</h3></div>
              <ul className="touch-list">
                {data.callbacks.slice(0, 6).map((item, idx) => (
                  <li key={idx}>
                    <div>
                      <strong>{String(item.job_type || "-")}</strong>
                      <p>{String(item.display_name || item.phone || item.external_user_id || "-")}</p>
                    </div>
                    <StatusBadge label={String(item.status || "-")} tone={statusTone(String(item.status || ""))} />
                  </li>
                ))}
                {data.callbacks.length === 0 && <li><p>Sem callbacks pendentes.</p></li>}
              </ul>
            </section>
          </section>
        )}

        {activeTab === "leads" && (
          <section className="stack">
            <section className="panel">
              <div className="panel-head">
                <h3>Mensagens recentes</h3>
                <select value={messageFilter} onChange={(e) => setMessageFilter(e.target.value)}>
                  <option value="all">Todos papéis</option>
                  <option value="user">Usuário</option>
                  <option value="assistant">Assistente</option>
                </select>
              </div>
              <ul className="touch-list">
                {filteredMessages.slice(0, 16).map((msg, i) => (
                  <li key={i}>
                    <div>
                      <strong>{String(msg.display_name || msg.phone || "-")}</strong>
                      <p>{String(msg.role || "mensagem")} - {dateTime(msg.created_at)}</p>
                    </div>
                    <button className="quick-btn" type="button">Analisar</button>
                  </li>
                ))}
              </ul>
            </section>
          </section>
        )}

        {activeTab === "atendimento" && (
          <section className="stack">
            <section className="panel">
              <div className="panel-head">
                <h3>Fila de jobs</h3>
                <select value={jobStatusFilter} onChange={(e) => setJobStatusFilter(e.target.value)}>
                  <option value="all">Todos status</option>
                  <option value="pending">Pending</option>
                  <option value="failed">Failed</option>
                  <option value="completed">Completed</option>
                </select>
              </div>
              <ul className="touch-list">
                {filteredJobs.slice(0, 20).map((job, i) => (
                  <li key={i}>
                    <div>
                      <strong>{String(job.job_type || "-")}</strong>
                      <p>{String(job.display_name || job.external_user_id || "-")} - {dateTime(job.run_at)}</p>
                    </div>
                    <StatusBadge label={String(job.status || "-")} tone={statusTone(String(job.status || ""))} />
                  </li>
                ))}
              </ul>
            </section>
          </section>
        )}

        {activeTab === "agenda" && (
          <section className="panel">
            <div className="panel-head"><h3>Agenda de atendimento</h3></div>
            <ul className="touch-list">
              {data.appointments.slice(0, 16).map((appointment, i) => (
                <li key={i}>
                  <div>
                    <strong>{String(appointment.display_name || appointment.phone || "-")}</strong>
                    <p>{String(appointment.window_label || "-")} - {dateTime(appointment.created_at)}</p>
                  </div>
                  <StatusBadge label={String(appointment.status || "-")} tone={statusTone(String(appointment.status || ""))} />
                </li>
              ))}
            </ul>
          </section>
        )}

        {activeTab === "financeiro" && (
          <section className="stack">
            <section className="grid-two">
              <ChartPanel title="Tendência de entradas e saídas">
                <ResponsiveContainer width="100%" height={250}>
                  <LineChart data={financeTrend}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="date" />
                    <YAxis />
                    <Tooltip />
                    <Legend />
                    <Line type="monotone" dataKey="income" stroke="#16a34a" strokeWidth={2} />
                    <Line type="monotone" dataKey="expense" stroke="#dc2626" strokeWidth={2} />
                  </LineChart>
                </ResponsiveContainer>
              </ChartPanel>
              <ChartPanel title="Mix de despesas por categoria">
                <ResponsiveContainer width="100%" height={250}>
                  <PieChart>
                    <Pie data={expenseByCategory} dataKey="value" nameKey="name" outerRadius={86}>
                      {expenseByCategory.map((entry, idx) => (
                        <Cell key={entry.name} fill={CHART_COLORS[idx % CHART_COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </ChartPanel>
            </section>

            <section className="panel">
              <div className="panel-head">
                <h3>Lançamento manual</h3>
                <select value={entryOriginFilter} onChange={(e) => setEntryOriginFilter(e.target.value)}>
                  <option value="all">Origem: todas</option>
                  <option value="manual">Origem: manual</option>
                  <option value="agente">Origem: agente</option>
                </select>
              </div>
              <form className="form-grid" onSubmit={submitFinanceEntry}>
                <label>Tipo
                  <select value={financeForm.entry_type} onChange={(e) => setFinanceForm((p) => ({ ...p, entry_type: e.target.value as "income" | "expense" }))}>
                    <option value="income">Entrada</option>
                    <option value="expense">Saída</option>
                  </select>
                </label>
                <label>Categoria
                  <select value={financeForm.category} onChange={(e) => setFinanceForm((p) => ({ ...p, category: e.target.value }))}>
                    <option value="despesa_operacional">Despesa operacional</option>
                    <option value="despesa_funcionario_diaria">Despesa diária funcionário</option>
                    <option value="receita_manual">Receita manual</option>
                    <option value="ajuste">Ajuste</option>
                  </select>
                </label>
                <label>Valor (R$)
                  <input inputMode="decimal" value={financeForm.amount} onChange={(e) => setFinanceForm((p) => ({ ...p, amount: e.target.value }))} />
                </label>
                <label>Data
                  <input type="date" value={financeForm.due_date} onChange={(e) => setFinanceForm((p) => ({ ...p, due_date: e.target.value }))} />
                </label>
                <label>Funcionário
                  <input value={financeForm.employee_name} onChange={(e) => setFinanceForm((p) => ({ ...p, employee_name: e.target.value }))} />
                </label>
                <label className="full-row">Observação
                  <textarea rows={3} value={financeForm.description} onChange={(e) => setFinanceForm((p) => ({ ...p, description: e.target.value }))} />
                </label>
                <button type="submit" className="primary-btn" disabled={financeBusy}>{financeBusy ? "Salvando..." : "Salvar lançamento"}</button>
                {financeFeedback ? <p className="feedback-text">{financeFeedback}</p> : null}
              </form>
            </section>

            <section className="panel">
              <div className="panel-head"><h3>Entradas recentes</h3></div>
              <ul className="touch-list">
                {entriesFiltered.slice(0, 20).map((entry, i) => {
                  const metadata = (entry.metadata as Dict | undefined) || {};
                  return (
                    <li key={i}>
                      <div>
                        <strong>{String(entry.category || "-")} - {money(entry.amount)}</strong>
                        <p>{String(entry.entry_type || "-")} | {String(metadata.source || "agente")} | {String(metadata.employee_name || "-")}</p>
                      </div>
                      <StatusBadge label={String(entry.status || "-")} tone={statusTone(String(entry.status || ""))} />
                    </li>
                  );
                })}
              </ul>
              <p className="section-hint">Previsão do funil: {money(forecast.quoted_total)} | Leads no funil: {String(forecast.leads_total || 0)}</p>
            </section>
          </section>
        )}
      </div>

      <nav className="mobile-bottom-nav" aria-label="Navegação rápida mobile">
        {TABS.map((tab) => (
          <button key={tab.id} type="button" className={activeTab === tab.id ? "tab-btn is-active" : "tab-btn"} onClick={() => setActiveTab(tab.id)}>
            {tab.label}
          </button>
        ))}
      </nav>
    </main>
  );
}
