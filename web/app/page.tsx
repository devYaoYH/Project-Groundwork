import Link from "next/link";

const vocabulary = [
  {
    term: "Environment",
    description: "A versioned study setting: its roles, parameters, measures, and item bank.",
  },
  {
    term: "Design",
    description: "The authored plan for what to vary, which participants to use, and how many observations to run.",
  },
  {
    term: "Experiment",
    description: "The locked, reproducible version of a design that is ready to launch.",
  },
  {
    term: "Cell",
    description: "One combination of an experiment's factor levels.",
  },
  {
    term: "Replication",
    description: "One planned observation within a cell, used to compare outcomes across the same conditions.",
  },
  {
    term: "Execution",
    description: "A physical run of a replication. A later execution can retry the same planned work.",
  },
  {
    term: "Episode trace",
    description: "The recorded evidence from one execution: events, configuration, metrics, and provenance.",
  },
];

const workflow = [
  {
    title: "Choose an environment",
    description: "Review the available study settings, their parameters, participants, and item data.",
    href: "/environments/",
    action: "Browse environments",
  },
  {
    title: "Author and validate a design",
    description: "Select the conditions to vary, set replications, and review the compiled plan before locking it.",
    href: "/design/",
    action: "Open design editor",
  },
  {
    title: "Lock and launch the experiment",
    description: "Use the experiment record to preserve the reviewed plan, launch it, and follow its progress.",
    href: "/experiments/",
    action: "View experiments",
  },
  {
    title: "Compare cell evidence",
    description: "Inspect locked factor levels, replication progress, and native metric summaries across cells.",
    href: "/experiments/",
    action: "Compare experiments",
  },
  {
    title: "Inspect an episode trace",
    description: "Search individual replications and open their execution evidence when you need the full record.",
    href: "/episodes/",
    action: "Browse episode traces",
  },
];

export default function Home() {
  return (
    <>
      <header className="page-heading onboarding-heading">
        <div>
          <p className="eyebrow">Researcher primer</p>
          <h1>From a question to recorded evidence.</h1>
          <p>
            Groundwork helps you define a controlled study, run its planned observations, and inspect the
            evidence each execution produces.
          </p>
        </div>
        <Link className="button button-primary" href="/environments/">Start with an environment</Link>
      </header>

      <section className="card onboarding-card onboarding-model" aria-labelledby="research-model-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">The research model</p>
            <h2 id="research-model-heading">The vocabulary, in order</h2>
          </div>
          <p>Each term narrows the work from a reusable setting to the evidence for one physical run.</p>
        </div>
        <ol className="vocabulary-path">
          {vocabulary.map((item, index) => (
            <li key={item.term}>
              <span className="vocabulary-number">{String(index + 1).padStart(2, "0")}</span>
              <strong>{item.term}</strong>
              <span>{item.description}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="card onboarding-card" aria-labelledby="first-run-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">First happy path</p>
            <h2 id="first-run-heading">Run a study you can revisit</h2>
          </div>
          <p>Follow these steps in order for a complete first pass through the control plane.</p>
        </div>
        <ol className="onboarding-steps">
          {workflow.map((step, index) => (
            <li key={step.title} className="onboarding-step">
              <span className="onboarding-step-number">{index + 1}</span>
              <div>
                <h3>{step.title}</h3>
                <p>{step.description}</p>
                <Link className="text-link" href={step.href}>{step.action}</Link>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
