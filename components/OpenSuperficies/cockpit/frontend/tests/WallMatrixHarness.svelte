<script lang="ts">
 import WorkspaceMatrix from '../src/lib/workspace/WorkspaceMatrix.svelte';
 import XpraFrame from '../src/lib/cards/XpraFrame.svelte';
 import type { CockpitState, SlotBlock } from '../src/lib/types/hypervisor';
 let {doc}:{doc:CockpitState}=$props();
</script>
{#snippet superficie(block:SlotBlock)}
 <XpraFrame puerto={block.xpra_port} tamano={{w:1264,h:1024}} titulo={`Operator view ${block.slot_id}`} />
{/snippet}
<WorkspaceMatrix {doc} bytes={JSON.stringify(doc)} {superficie} />
