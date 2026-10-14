(() => {
  const root = document.getElementById('research-chat');
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = 'true';
  const form = root.querySelector('#chat-form');
  const input = root.querySelector('#chat-question');
  const messages = root.querySelector('#chat-messages');
  const submit = root.querySelector('#chat-submit');
  const clear = root.querySelector('#chat-clear');
  let history = [];
  let busy = false;
  root.querySelectorAll('.chat-starter').forEach(button => button.addEventListener('click', () => {
    input.value = button.textContent.trim();
    input.focus();
  }));
  clear.addEventListener('click', () => {
    if (busy) return;
    history = [];
    messages.replaceChildren();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || busy) return;
    busy = true;
    submit.disabled = clear.disabled = true;
    const user = document.createElement('p');
    user.className = 'text-sm rounded-lg bg-blue-50 dark:bg-blue-950 p-3';
    user.textContent = question;
    const answer = document.createElement('div');
    answer.className = 'text-sm rounded-lg border border-slate-200 dark:border-slate-800 p-3';
    answer.textContent = 'Finding supporting evidence…';
    messages.append(user, answer);
    try {
      const response = await fetch(`/research/${encodeURIComponent(root.dataset.ticker)}/chat`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question, history: history.slice(-12)})
      });
      if (!response.ok) throw new Error('Could not submit the question. Please retry.');
      const job = await response.json();
      if (job.state === 'disabled') throw new Error(job.error);
      let result;
      for (let attempt = 0; attempt < 300; attempt++) {
        const poll = await fetch(`/ai/jobs/${encodeURIComponent(job.job_key)}`);
        if (!poll.ok) throw new Error('Could not load this answer. Please retry.');
        result = await poll.json();
        if (result.state === 'error') throw new Error(result.error);
        if (result.state === 'ready') break;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      if (result.state !== 'ready') throw new Error('This answer is taking longer than expected. Retry shortly.');
      // HTML is formatted and escaped by the server's safe Markdown renderer.
      answer.innerHTML = result.html;
      history.push({role: 'user', content: question},
                   {role: 'assistant', content: result.result.content.slice(0, 6000)});
      history = history.slice(-12);
      input.value = '';
    } catch (error) {
      answer.textContent = error.message || 'Could not prepare an answer. Please retry.';
    } finally {
      busy = false;
      submit.disabled = clear.disabled = false;
    }
  });
})();
