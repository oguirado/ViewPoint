<script lang="ts">
  import { setContext } from 'svelte';
  import XpraFrame from '../src/lib/cards/XpraFrame.svelte';
  import { FRAMEBUFFER_CONTEXT, type FramebufferContext } from '../src/lib/cards/visor';
  import { FRAMEBUFFER } from '../src/lib/aiops/splittree';
  setContext<FramebufferContext>(FRAMEBUFFER_CONTEXT,()=>FRAMEBUFFER);
</script>
<XpraFrame puerto={10020} tamano={{w:1264,h:1024}} titulo="Operator view srf-ng-0" />
