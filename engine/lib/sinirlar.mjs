// 4c-4: the single source of output limits is sinirlar.json (Python sinirlar.py reads the same file).
import { readFileSync } from 'node:fs';

export const SINIRLAR = JSON.parse(readFileSync(new URL('./sinirlar.json', import.meta.url), 'utf8'));

// Role: the engine's phase role (CADRE_ROL) or the plan agent; every other call gets the lane cap. On the single attempt after a truncation
// the engine sets CADRE_CIKTI_KAT=2; the caller clips the result to the model's own cap.
export function rolCiktisi(rol, ajan) {
  const cikti = SINIRLAR.cikti;
  const kat = process.env.CADRE_CIKTI_KAT === '2' ? 2 : 1;
  if (ajan === 'plan' || ajan === 'cadre-plan' || rol === 'plan') return cikti.plan * kat;
  return (cikti[rol] ?? cikti.serit) * kat;
}

// Visible output request: with Anthropic budget-type thinking max_tokens includes the thinking budget too; the gate measures the difference (16-4).
export function istenenCikti(body) {
  const toplam = body.max_completion_tokens ?? body.max_tokens ?? body.max_output_tokens ?? body.generationConfig?.maxOutputTokens;
  const butce = body.thinking?.type === 'enabled' ? body.thinking.budget_tokens : 0;
  if (!Number.isSafeInteger(toplam)) return { toplam, gorunur: toplam };
  return { toplam, gorunur: Number.isSafeInteger(butce) && butce > 0 && butce < toplam ? toplam - butce : toplam };
}
