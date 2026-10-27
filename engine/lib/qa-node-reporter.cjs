const fs = require('node:fs');

module.exports = async function* (source) {
  for await (const event of source) {
    if (!['test:pass', 'test:fail'].includes(event.type)) continue;
    const data = event.data;
    if (data.details?.type === 'suite' && event.type === 'test:pass' && !data.skip && !data.todo) continue;
    if (!data.file || data.name === data.file || data.name === require('node:path').basename(data.file)) continue;
    const id = `${data.file}:${data.line}:${data.column}:${data.testNumber}::${data.name}`;
    const common = {id, pid: process.pid, token: process.env.CADRE_QA_TOKEN, runner: 'node'};
    const status = data.skip || data.todo ? 'SKIP' : event.type === 'test:pass' ? 'PASS' : 'FAIL';
    const rows = [
      {...common, event: 'collect', path: data.file, name: data.name},
      {...common, event: 'result', status},
    ];
    const fd = fs.openSync(process.env.CADRE_QA_EVIDENCE,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_APPEND | fs.constants.O_NOFOLLOW, 0o600);
    try {
      if (fs.fstatSync(fd).size > 8000000) throw new Error('qa-evidence-limit');
      fs.writeSync(fd, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
    } finally {
      fs.closeSync(fd);
    }
  }
};
