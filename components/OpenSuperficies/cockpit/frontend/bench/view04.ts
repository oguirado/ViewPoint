// Bench page of VIEW-04 (dev server only, never built): the matrix standalone with its default fixtures.
import { mount } from 'svelte';
import '../src/app.css';
import WorkspaceMatrix from '../src/lib/workspace/WorkspaceMatrix.svelte';

mount(WorkspaceMatrix, { target: document.getElementById('bench')!, props: { standalone: true } });
