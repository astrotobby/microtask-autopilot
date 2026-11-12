const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const titleCase = (value) => String(value || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
const dateText = (value) => value ? new Date(value).toLocaleString([], {dateStyle:'medium', timeStyle:'short'}) : 'Not recorded';
const num = (value) => new Intl.NumberFormat().format(Number(value || 0));
const pill = (value, tone='blue') => `<span class="pill ${tone}">${esc(titleCase(value))}</span>`;
let data;

function platformTone(platform) {
  return platform.health === 'automated' ? 'auto' : platform.health === 'policy_monitor' ? 'policy' : 'manual';
}
function platformStatus(platform) {
  if (platform.health === 'automated') return pill('Automated discovery', 'green');
  if (platform.health === 'policy_monitor') return pill('Policy monitor', 'amber');
  return pill('Manual / review', 'blue');
}
function riskTone(label) { return label === 'HIGH_RISK' ? 'red' : label === 'REQUIRES_REVIEW' ? 'amber' : 'blue'; }
function eligibilityTone(label) { return label === 'KENYA_CONFIRMED' || label === 'WORLDWIDE_CONFIRMED' ? 'green' : label === 'EXPIRED' ? 'red' : 'amber'; }

function renderMetrics() {
  const s = data.summary;
  const items = [
    ['PLATFORMS TRACKED', num(s.platforms), '25 registry records', 'accent'],
    ['OPPORTUNITIES', num(s.opportunities_total), `${num(s.opportunities_open)} currently open`, 'accent'],
    ['AUTOMATED SOURCES', num(s.automated_discovery), 'OpenTrain public discovery', 'success'],
    ['NEEDS REVIEW', num(s.needs_review), 'Eligibility or evidence gap', 'review'],
    ['RECEIVED RECORDS', num(s.received_payment_records), 'Worker-recorded payments', 'danger']
  ];
  document.querySelector('#metrics').innerHTML = items.map(([label, value, note, cls]) => `<div class="metric ${cls}"><div class="metric-label">${label}</div><strong>${value}</strong><small>${note}</small></div>`).join('');
  document.querySelector('#kenyaCount').textContent = num(s.kenya_confirmed);
  document.querySelector('#worldwideCount').textContent = num(s.worldwide_confirmed);
  document.querySelector('#reviewCount').textContent = num(s.needs_review);
  document.querySelector('#riskCount').textContent = num(s.high_risk);
  document.querySelector('#receivedRecords').textContent = num(s.received_payment_records);
  document.querySelector('#receivedTotal').textContent = s.received_payment_total ? num(s.received_payment_total) : '0 recorded';
  document.querySelector('#snapshotDate').textContent = `Snapshot ${dateText(data.generated_at)}`;
  document.querySelector('#commitRef').textContent = `Source ${data.source_commit}`;
  document.querySelector('#dataNote').textContent = `${num(s.risk_review)} source/payment review flags · ${num(s.manual_or_unverified)} manual or unverified platform records`;
}

function renderOpportunities() {
  const query = (document.querySelector('#opportunitySearch').value || '').toLowerCase();
  const eligibility = document.querySelector('#eligibilityFilter').value;
  const filtered = data.top_opportunities.filter((o) => {
    const text = `${o.title} ${o.source} ${o.platform_id}`.toLowerCase();
    return (!query || text.includes(query)) && (!eligibility || o.eligibility_class === eligibility);
  });
  const platformName = new Map(data.platforms.map((p) => [p.id, p.name]));
  document.querySelector('#opportunityRows').innerHTML = filtered.length ? filtered.map((o) => `<tr>
    <td><div class="opp-title">${esc(o.title || 'Untitled listing')}</div><div class="opp-sub">${esc(platformName.get(o.platform_id) || o.source || 'Supplementary feed')} · ${esc((o.summary || '').slice(0, 120))}</div></td>
    <td>${pill(o.eligibility_class || 'Unclassified', eligibilityTone(o.eligibility_class))}</td>
    <td><span class="score">${esc(o.relevance_score ?? '—')}</span></td>
    <td>${esc(o.payment_text || 'Not disclosed')}</td>
    <td>${pill(o.risk_label || 'Unassessed', riskTone(o.risk_label))}<div class="opp-sub">score ${esc(o.risk_score ?? '—')}</div></td>
    <td>${o.url ? `<a href="${esc(o.apply_url || o.url)}" target="_blank" rel="noreferrer">Open source ↗</a>` : 'No link'}</td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty-cell">No records match this filter.</td></tr>';
  document.querySelector('#opportunityFoot').textContent = `${filtered.length} of ${data.top_opportunities.length} top records shown. Scores rank suitability; they do not guarantee acceptance or earnings.`;
}

function renderPlatforms() {
  const query = (document.querySelector('#platformSearch').value || '').toLowerCase();
  const health = document.querySelector('#healthFilter').value;
  const filtered = data.platforms.filter((p) => {
    const text = `${p.name} ${p.category} ${p.id}`.toLowerCase();
    const healthMatch = !health || (health === 'manual_only' ? !['automated', 'policy_monitor'].includes(p.health) : p.health === health);
    return (!query || text.includes(query)) && healthMatch;
  });
  document.querySelector('#platformGrid').innerHTML = filtered.map((p) => `<article class="platform-card ${platformTone(p)}">
    <div class="platform-top"><div><h3>${esc(p.name)}</h3><div class="platform-type">${esc(titleCase(p.category))}</div></div>${platformStatus(p)}</div>
    <p>${esc(p.notes || 'No platform note recorded.')}</p>
    <div class="platform-tags"><span>Kenya: ${esc(titleCase(p.eligibility || 'unverified'))}</span><span>Payout: ${esc(titleCase(p.payment_status || 'unverified'))}</span></div>
    <div class="platform-tags"><span>Discovery: ${esc(titleCase(p.public_discovery || 'none'))}</span><span>Automation: ${esc(titleCase(p.automation_permitted || 'none'))}</span></div>
    ${p.official_url ? `<a class="platform-link" href="${esc(p.official_url)}" target="_blank" rel="noreferrer">Open official site ↗</a>` : '<span class="platform-link">Official URL requires confirmation</span>'}
  </article>`).join('') || '<div class="empty-cell">No platforms match this filter.</div>';
}

async function boot() {
  try {
    const response = await fetch('/api/data', {cache:'no-store'});
    if (!response.ok) throw new Error('Snapshot unavailable');
    data = await response.json();
    renderMetrics(); renderOpportunities(); renderPlatforms();
    document.querySelector('#opportunitySearch').addEventListener('input', renderOpportunities);
    document.querySelector('#eligibilityFilter').addEventListener('change', renderOpportunities);
    document.querySelector('#platformSearch').addEventListener('input', renderPlatforms);
    document.querySelector('#healthFilter').addEventListener('change', renderPlatforms);
  } catch (error) {
    document.querySelector('.shell').innerHTML = `<div class="boundary-card"><div class="section-kicker">SNAPSHOT UNAVAILABLE</div><h2>The hosted data snapshot could not be loaded.</h2><p>${esc(error.message)}. Try refreshing the page.</p></div>`;
  }
}
boot();
