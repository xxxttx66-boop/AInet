import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import vm from 'node:vm';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'frontend');
let count = 0;
for (const name of fs.readdirSync(root)) {
  const filename = path.join(root, name);
  if (name.endsWith('.js')) {
    execFileSync(process.execPath, ['--check', filename], { stdio: 'inherit' });
    count++;
  } else if (name.endsWith('.html')) {
    const source = fs.readFileSync(filename, 'utf8');
    for (const match of source.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
      if (/\bsrc\s*=/i.test(match[1]) || /\btype\s*=\s*["'](?:application|text)\/json/i.test(match[1])) continue;
      new vm.Script(match[2], { filename: `${name}:inline` });
      count++;
    }
  }
}
console.log(`${count} frontend scripts passed syntax checks.`);
