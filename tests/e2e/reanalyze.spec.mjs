/**
 * End-to-end checks against the deployed rhythm dashboard.
 *
 * These exist because the reanalyze defects were invisible to the Python suite: the
 * button fired correctly, returned 200, and logged no error — it just could not change
 * anything, and a failed analysis left no way to retry. Only driving the real page
 * catches that class of bug.
 *
 * Run:  node tests/e2e/reanalyze.spec.mjs [baseURL]
 * Default baseURL is the LAN deployment on scott-server.
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://192.168.68.53:8080';
const CAT = 'bridget';        // jammiest feeder: 1.07 jams/day, so a trim is expected
const CLEAN_CAT = 'ricco';    // 0.16 jams/day: inside the dead band, must NOT be trimmed

let failures = 0;
const consoleErrors = [];

function check(name, condition, detail = '') {
  if (condition) {
    console.log(`  PASS  ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

async function pickCat(page, name) {
  await page.getByRole('button', { name: new RegExp(name) }).click();
  // the analysis round trip is slow (live PetLibro cloud); wait for the plan card
  await page.waitForSelector('#proposed-col .stepper-input', { timeout: 120000 });
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));

  console.log(`\n=== ${BASE} ===`);

  // ---- the API contract the UI depends on -----------------------------------
  console.log('\n[api] analysis payload');
  const api = await page.request.get(
    `${BASE}/api/pets/${CAT}/analysis?days=60`, { timeout: 180000 });
  check('analysis returns 200', api.ok(), `status ${api.status()}`);
  const body = await api.json();
  check('payload carries trim', body.trim !== undefined);
  check('payload carries evidence', body.evidence !== undefined);
  check('trim reports a jam rate', typeof body.trim?.jam_rate === 'number');
  check('evidence reports a verdict', typeof body.evidence?.verdict === 'string');

  const currentTotal = body.daily_total_portions;
  const recommended = (body.recommended_schedule || [])
    .reduce((s, r) => s + r.portions, 0);

  // The whole point of the change: a jammy feeder must be recommended LESS.
  check('jammy cat is recommended below its current total',
    recommended < currentTotal, `${currentTotal} -> ${recommended}`);
  check('recommendation respects the 8/day floor',
    recommended >= 8, `got ${recommended}`);
  check('no meal is below 1 portion',
    (body.recommended_schedule || []).every((r) => r.portions >= 1));

  // ---- the page renders the new plan ----------------------------------------
  console.log('\n[ui] plan is rendered and explained');
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await pickCat(page, CAT);

  const noteText = await page.locator('.banner-success, .banner-info').first()
    .textContent().catch(() => '');
  check('a plan banner is shown', !!noteText && noteText.trim().length > 0);
  check('the banner names the daily total change or says no change',
    /portions\/day|No change|No trim yet|recomputed/i.test(noteText || ''),
    JSON.stringify((noteText || '').slice(0, 90)));

  const jamTags = await page.locator('.jam-tag').count();
  check('jam counts are shown next to meals', jamTags > 0, `${jamTags} tags`);

  // ---- reanalyze gives visible feedback -------------------------------------
  console.log('\n[ui] reanalyze reports that it ran');
  const reBtn = page.getByRole('button', { name: /Reanalyze/ });
  await reBtn.click();
  // the in-flight state is the fix for "the button looks dead"
  const busy = await page.getByRole('button', { name: /Reading history/ })
    .waitFor({ timeout: 15000 }).then(() => true).catch(() => false);
  check('button shows an in-flight state', busy);
  await page.waitForSelector('#proposed-col .stepper-input', { timeout: 120000 });
  check('plan is still rendered after reanalyze',
    (await page.locator('#proposed-col .stepper-input').count()) > 0);

  // ---- the steppers cannot delete a meal ------------------------------------
  console.log('\n[ui] steppers floor at 1');
  const firstInput = page.locator('#proposed-col .stepper-input').first();
  const dec = page.locator('#proposed-col .stepper-btn[data-action="dec"]').first();
  for (let i = 0; i < 12; i++) await dec.click();
  const floored = await firstInput.inputValue();
  check('a meal never reaches 0 portions', Number(floored) >= 1, `value ${floored}`);

  // ---- a clean feeder is left alone ----------------------------------------
  console.log(`\n[api] ${CLEAN_CAT} is inside the dead band`);
  const clean = await page.request.get(
    `${BASE}/api/pets/${CLEAN_CAT}/analysis?days=60`, { timeout: 180000 });
  const cleanBody = await clean.json();
  check('clean feeder is not trimmed',
    cleanBody.trim?.applied === 0, `applied ${cleanBody.trim?.applied}`);
  const cleanRec = (cleanBody.recommended_schedule || [])
    .reduce((s, r) => s + r.portions, 0);
  check('clean feeder keeps its daily total',
    cleanRec === cleanBody.daily_total_portions,
    `${cleanBody.daily_total_portions} -> ${cleanRec}`);

  // ---- no console errors during NORMAL operation ---------------------------
  // Asserted here, before the deliberate-failure scenario below: aborting a request
  // logs an ERR_FAILED by design, so counting it would make the check meaningless.
  console.log('\n[ui] console hygiene during normal use');
  check('no console errors', consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(' | '));

  // ---- a failed analysis is recoverable ------------------------------------
  console.log('\n[ui] a failed analysis still offers a retry');
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  // Force the analysis call to fail so the error branch renders.
  await page.route('**/api/pets/*/analysis*', (route) => route.abort('failed'));
  await page.getByRole('button', { name: new RegExp(CAT) }).click();
  const retry = await page.getByRole('button', { name: /Try again/ })
    .waitFor({ timeout: 30000 }).then(() => true).catch(() => false);
  check('error state renders a Try again button', retry);

  await browser.close();

  console.log(failures === 0
    ? '\nALL CHECKS PASSED\n'
    : `\n${failures} CHECK(S) FAILED\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error('\nharness error:', e);
  process.exit(2);
});
