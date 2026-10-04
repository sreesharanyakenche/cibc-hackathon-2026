import { useEffect, useState } from "react";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { Badge } from "./components/ui";
import AskPage from "./pages/AskPage";
import AuditPage from "./pages/AuditPage";
import CustomerPage from "./pages/CustomerPage";
import DecisionPage from "./pages/DecisionPage";
import QueuePage from "./pages/QueuePage";

function Header() {
  const [health, setHealth] = useState<string>("…");
  const [llm, setLlm] = useState(false);
  useEffect(() => {
    api.health().then((h) => { setHealth(h.status); setLlm(!!h.llm_enabled); }).catch(() => setHealth("unreachable"));
  }, []);
  return (
    <header className="top">
      <Link to="/" className="brand"><span>◆</span> CodeBlooded <small>Collections Intelligence</small></Link>
      <nav>
        <NavLink to="/" end>Agent queue</NavLink>
        <NavLink to="/ask">Ask the data</NavLink>
        <NavLink to="/audit">Decisions &amp; audit</NavLink>
      </nav>
      <div>
        <Badge tone={health === "ok" ? "green" : "red"}>api {health}</Badge>{" "}
        <Badge tone={llm ? "blue" : "gray"}>{llm ? "LLM planner on" : "rule-based (no LLM)"}</Badge>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <HashRouter>
      <Header />
      <main>
        <Routes>
          <Route path="/" element={<QueuePage />} />
          <Route path="/customers/:id" element={<CustomerPage />} />
          <Route path="/decision/:id" element={<DecisionPage />} />
          <Route path="/ask" element={<AskPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="*" element={<p>Page not found. <Link to="/">Back to the queue</Link></p>} />
        </Routes>
      </main>
      <footer>Synthetic Maple Bank data · the model supplies one number; every decision about what may happen to a customer is rule-based, logged and open to human review · demo build</footer>
    </HashRouter>
  );
}
