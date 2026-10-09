// Real-browser check of the result card: every fixture at 1280 and 390 wide, light and dark, plus the actions.
// Needs Playwright (not a dependency of this package) and a static server on the repository root:
//   python -m http.server 8799 &   (from the repository root)
//   NODE_PATH=/path/to/node_modules node tests/widget_harness/check.mjs http://127.0.0.1:8799 /tmp/card-shots
import { createRequire } from "node:module";
import assert from "node:assert/strict";

const { chromium } = createRequire(import.meta.url)(process.env.NODE_PATH + "/playwright");
const [base = "http://127.0.0.1:8799", out = "card-shots"] = process.argv.slice(2);
const ROWS = { full: 8, warning: 3, empty: 0, grant: 3 };
// CHANNEL=chrome uses the installed Google Chrome instead of a Playwright download.
const browser = await chromium.launch(process.env.CHANNEL ? { channel: process.env.CHANNEL } : {});

async function open(fixture, width, theme, mode = "bridge") {
  const page = await browser.newPage({ viewport: { width, height: 800 }, colorScheme: theme });
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${base}/tests/widget_harness/index.html?fixture=${fixture}&theme=${theme}&mode=${mode}`);
  const card = page.frameLocator("#card");
  return { page, card, errors };
}

for (const fixture of Object.keys(ROWS)) {
  for (const width of [1280, 390]) {
    for (const theme of ["light", "dark"]) {
      const { page, card, errors } = await open(fixture, width, theme);
      await card.locator("#body:not([aria-busy])").waitFor();
      assert.equal(await card.locator(".rows > li").count(), ROWS[fixture], fixture);
      const frame = page.frames()[1];
      const fit = await frame.evaluate(() => [document.documentElement.scrollWidth, innerWidth,
        document.documentElement.getAttribute("data-theme")]);
      assert.ok(fit[0] <= fit[1], `${fixture} ${width} ${theme}: horizontal overflow ${fit}`);
      assert.equal(fit[2], theme, "the host theme is applied");
      // The frame is sized to the card: no inner scroll.
      await frame.waitForFunction(() => document.documentElement.scrollHeight - innerHeight <= 2, null,
        { timeout: 3000 }).catch(() => assert.fail(`${fixture} ${width} ${theme}: the frame never fit the card`));
      assert.deepEqual(errors, [], "no console errors, no CSP violations");
      // Chromium paints an out-of-process frame only inside the viewport: grow it to the page first.
      await page.setViewportSize({ width, height: await page.evaluate(() => document.body.scrollHeight) });
      await page.waitForTimeout(200);
      await page.screenshot({ path: `${out}/${fixture}-${width}-${theme}.png` });
      await page.close();
    }
  }
}

// Actions over the MCP Apps bridge: a row asks the model; the link goes through ui/open-link, never a tool call.
{
  const { page, card } = await open("full", 390, "light");
  await card.locator(".rows > li").first().waitFor();
  await card.locator("button.main").first().click();
  await card.getByRole("link", { name: /Open Roof replacement, building 1 on MatchAwards/ }).click();
  const sent = await page.evaluate(() => window.messages.filter((m) => m.method && m.method.startsWith("ui/")));
  const ask = sent.find((m) => m.method === "ui/message");
  assert.deepEqual(ask.params, { role: "user", content: [{ type: "text",
    text: "Show details for Roof replacement, building 1 (id 117402307943537001)" }] });
  const link = sent.find((m) => m.method === "ui/open-link");
  assert.deepEqual(link.params, { url: "https://matchawards.com/a/posts/1" });
  assert.ok(!(await page.evaluate(() => window.messages.some((m) => m.method === "tools/call"))));
  // Links only to matchawards.com pages: the javascript: and the missing url get none.
  assert.equal(await card.locator(".rows > li").nth(5).locator("a").count(), 0);
  assert.equal(await card.locator(".rows > li").nth(6).locator("a").count(), 0);
  assert.equal(await card.locator(".foot").textContent(), "4 more not shown here, and more on MatchAwards.");
  await page.close();
}

// The window.openai compatibility layer: toolOutput renders, the actions use its methods.
{
  const { page, card, errors } = await open("grant", 1280, "dark", "openai");
  await card.locator(".rows > li").first().waitFor();
  assert.equal(await card.locator(".due").first().textContent(), "No fixed deadline");
  await card.locator("button.main").first().click();
  await card.getByRole("link").first().click();
  const calls = await page.evaluate(() => window.messages.filter((m) => m.openai));
  assert.deepEqual(calls, [
    { openai: "sendFollowUpMessage", args: { prompt: "Show details for Broadband infrastructure deployment grants " +
      "(id 117402307943537031)" } },
    { openai: "openExternal", args: { href: "https://matchawards.com/a/posts/31" } },
  ]);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: `${out}/grant-1280-dark-openai.png`, fullPage: true });
  await page.close();
}

// A late result: the skeleton first, then the rows. No result at all: the skeleton resolves to a note.
{
  const { page, card } = await open("warning", 390, "light", "late");
  await card.locator("#body[aria-busy]").waitFor();
  await page.screenshot({ path: `${out}/late-skeleton-390-light.png`, fullPage: true });
  await card.locator(".rows > li").first().waitFor();
  await page.close();
}
{
  const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
  await page.clock.install();
  await page.goto(`${base}/tests/widget_harness/index.html?fixture=full&mode=none`);
  await page.frameLocator("#card").locator("#body[aria-busy]").waitFor();
  await page.clock.runFor(21000);
  assert.equal(await page.frameLocator("#card").locator(".note").textContent(),
    "Results unavailable here. See the reply above.");
  await page.close();
}

await browser.close();
console.log("card check passed");
