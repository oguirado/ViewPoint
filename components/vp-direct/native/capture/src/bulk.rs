// SPDX-License-Identifier: AGPL-3.0-only
use std::{
    collections::{HashMap, VecDeque},
    ffi::{c_int, c_void},
    mem::{self, size_of},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    panic::{self, AssertUnwindSafe},
    ptr::{self, NonNull},
    slice,
    sync::{Mutex, MutexGuard, OnceLock},
};

const OK: i32 = 0;
const AGAIN: i32 = 1;
const INVALID: i32 = -1;
const DEADLINE: i32 = -2;
const BROKEN: i32 = -3;
const STALE: i32 = -4;
const ALREADY_RELEASED: i32 = -5;
const ABI_MISMATCH: i32 = -6;
const ABI: u32 = 1;
const BYTES: u32 = 1;
const FRAME: u32 = 2;
const END: u32 = 3;
const ORDERED: u32 = 1;
const LATEST: u32 = 2;
const HAS_PRIORITY: u32 = 1;
const HAS_EXPECTED_ORDER: u32 = 2;
const CAPACITY: usize = 32;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Intent {
    consumer_id: u64,
    deadline_monotonic_ns: u64,
    affinity_group: u64,
    expected_order: u64,
    priority: i32,
    flags: u32,
}

impl Intent {
    fn validate(self) -> Result<(), i32> {
        if self.consumer_id == 0
            || self.affinity_group == 0
            || self.deadline_monotonic_ns == 0
            || self.flags & HAS_PRIORITY == 0
            || self.flags & !(HAS_PRIORITY | HAS_EXPECTED_ORDER) != 0
        {
            return Err(INVALID);
        }
        self.check_deadline()
    }

    fn check_deadline(self) -> Result<(), i32> {
        let now = unsafe { vp_bulk_monotonic_ns() };
        if now == u64::MAX {
            return Err(BROKEN);
        }
        if now >= self.deadline_monotonic_ns {
            return Err(DEADLINE);
        }
        Ok(())
    }

    fn check_order(self, first: &Block) -> Result<(), i32> {
        if self.flags & HAS_EXPECTED_ORDER != 0 && self.expected_order != first.lease.reference {
            return Err(STALE);
        }
        Ok(())
    }
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct Lease {
    session_id: u64,
    lane_id: u64,
    generation: u64,
    reference: u64,
    epoch: u64,
}

type Series = (u64, u64, u64);

impl Lease {
    fn series(self) -> Series {
        (self.lane_id, self.generation, self.epoch)
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Block {
    lease: Lease,
    offset: u64,
    length: u64,
    skipped: u64,
    kind: u32,
    width: u32,
    height: u32,
    row_stride: u32,
    format_id: u32,
    fd: i32,
}

impl Block {
    fn extent(self) -> Result<usize, i32> {
        let extent = self
            .offset
            .checked_add(self.length)
            .filter(|&size| size <= isize::MAX as u64)
            .ok_or(INVALID)?;
        let geometry =
            (self.width == 0 && self.height == 0 && self.row_stride == 0 && self.format_id == 0)
                || self.kind == FRAME;
        if !geometry || self.skipped != 0 {
            return Err(INVALID);
        }
        match self.kind {
            END if self.fd == -1 && extent == 0 => {}
            BYTES if self.fd >= 0 => {}
            FRAME
                if self.fd >= 0
                    && self.format_id == super::RGB24
                    && self.width > 0
                    && self.height > 0
                    && u64::from(self.row_stride) >= u64::from(self.width) * 3
                    && self.length == u64::from(self.row_stride) * u64::from(self.height) => {}
            _ => return Err(INVALID),
        }
        Ok(extent as usize)
    }
}

#[repr(C)]
pub struct View {
    data: *const u8,
    length: u64,
}

impl View {
    const EMPTY: Self = Self {
        data: ptr::null(),
        length: 0,
    };
}

#[repr(C)]
pub struct Api {
    abi_version: u32,
    struct_size: u32,
    context: *mut c_void,
    publish_batch:
        unsafe extern "C" fn(*mut c_void, u64, *const Intent, *const Block, u32, *mut u32) -> i32,
    borrow_batch: unsafe extern "C" fn(
        *mut c_void,
        u64,
        *const Intent,
        u32,
        *mut Block,
        u32,
        *mut u32,
    ) -> i32,
    cpu_view: unsafe extern "C" fn(*mut c_void, *const Lease, *mut View) -> i32,
    release: unsafe extern "C" fn(*mut c_void, *const Lease) -> i32,
    break_and_drain: unsafe extern "C" fn(*mut c_void, u64) -> i32,
}

extern "C" {
    fn vp_bulk_dup(original: c_int) -> c_int;
    fn vp_bulk_map(fd: c_int, extent: usize, nonempty: c_int, out: *mut *mut c_void) -> c_int;
    fn vp_bulk_unmap(address: *mut c_void, length: usize);
    fn vp_bulk_monotonic_ns() -> u64;
}

struct Mapping {
    address: NonNull<u8>,
    extent: usize,
}

// The mapping is read-only and the sealed backing cannot change. Ownership
// may move between threads, but the exact live lease pins the mapped address.
unsafe impl Send for Mapping {}

impl Drop for Mapping {
    fn drop(&mut self) {
        unsafe { vp_bulk_unmap(self.address.as_ptr().cast(), self.extent) };
    }
}

struct Payload {
    block: Block,
    mapping: Option<Mapping>,
    _fd: Option<OwnedFd>,
}

impl Payload {
    fn prepare(mut block: Block, extent: usize) -> Result<Self, i32> {
        if block.kind == END {
            return Ok(Self {
                block,
                mapping: None,
                _fd: None,
            });
        }
        let duplicate = unsafe { vp_bulk_dup(block.fd) };
        if duplicate < 0 {
            return Err(duplicate);
        }
        let fd = unsafe { OwnedFd::from_raw_fd(duplicate) };
        let mut address = ptr::null_mut();
        let status = unsafe {
            vp_bulk_map(
                fd.as_raw_fd(),
                extent,
                (block.length != 0).into(),
                &mut address,
            )
        };
        if status != OK {
            return Err(status);
        }
        let mapping = NonNull::new(address.cast()).map(|address| Mapping { address, extent });
        block.fd = fd.as_raw_fd();
        Ok(Self {
            block,
            mapping,
            _fd: Some(fd),
        })
    }

    fn view(&self) -> Result<View, i32> {
        if self.block.kind == END {
            return Err(INVALID);
        }
        if self.block.length == 0 {
            return Ok(View::EMPTY);
        }
        let mapping = self.mapping.as_ref().ok_or(BROKEN)?;
        // Validation established offset + length <= mapping.extent and no wrap.
        let data = unsafe { mapping.address.as_ptr().add(self.block.offset as usize) };
        Ok(View {
            data,
            length: self.block.length,
        })
    }
}

#[derive(Clone, Copy)]
struct Epoch {
    id: u64,
    ended: bool,
}

impl Epoch {
    fn advance(current: Option<Self>, block: Block) -> Result<Self, i32> {
        let id = block.lease.epoch;
        let ended = block.kind == END;
        if let Some(current) = current {
            if (!current.ended && id != current.id) || (current.ended && id <= current.id) {
                return Err(STALE);
            }
        }
        Ok(Self { id, ended })
    }
}

struct Lane {
    session_id: u64,
    consumer_id: u64,
    epoch: Option<Epoch>,
    pending: VecDeque<Payload>,
    leased: usize,
}

impl Lane {
    fn new(session_id: u64, consumer_id: u64) -> Self {
        Self {
            session_id,
            consumer_id,
            epoch: None,
            pending: VecDeque::with_capacity(CAPACITY),
            leased: 0,
        }
    }

    fn space(&self) -> usize {
        CAPACITY - self.pending.len() - self.leased
    }
}

#[derive(Default)]
struct SessionState {
    fenced: bool,
    preparing: bool,
    live: HashMap<Lease, Payload>,
    // Contiguous issue ordinals per series let same-epoch capture run without
    // a tombstone allocation for each individual frame.
    issued: HashMap<Series, u64>,
}

#[derive(Default)]
struct State {
    lanes: HashMap<u64, Lane>,
    sessions: HashMap<u64, SessionState>,
    fenced: bool,
}

impl State {
    fn fence(&mut self, affected: Option<u64>) {
        match affected {
            Some(id) => self.sessions.entry(id).or_default().fenced = true,
            None => {
                self.fenced = true;
                for session in self.sessions.values_mut() {
                    session.fenced = true;
                }
            }
        }
        for lane in self.lanes.values_mut() {
            if affected.is_none_or(|id| lane.session_id == id) {
                lane.pending.clear();
            }
        }
    }

    fn lane_for_publish(&self, lane_id: u64, session_id: u64) -> Result<Option<&Lane>, i32> {
        let Some(lane) = self.lanes.get(&lane_id) else {
            return Ok(None);
        };
        if lane.session_id == session_id {
            return Ok(Some(lane));
        }
        let old = self.sessions.get(&lane.session_id).ok_or(BROKEN)?;
        if !old.fenced || old.preparing || !old.live.is_empty() {
            return Err(STALE);
        }
        Ok(None)
    }
}

struct Context {
    owner: u32,
    state: Mutex<State>,
}

static CONTEXT: OnceLock<Context> = OnceLock::new();

impl Context {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poison| {
            let mut state = poison.into_inner();
            state.fence(None);
            self.state.clear_poison();
            state
        })
    }
}

struct Preparation<'a> {
    context: &'a Context,
    session: u64,
}

impl Drop for Preparation<'_> {
    fn drop(&mut self) {
        let mut state = self.context.lock();
        if std::thread::panicking() {
            state.fence(Some(self.session));
        }
        if let Some(session) = state.sessions.get_mut(&self.session) {
            session.preparing = false;
        }
    }
}

pub(super) fn caught<T>(action: impl FnOnce() -> T) -> Result<T, i32> {
    panic::catch_unwind(AssertUnwindSafe(action)).map_err(|payload| {
        // A custom panic payload can itself panic from Drop.
        if let Err(nested) = panic::catch_unwind(AssertUnwindSafe(|| drop(payload))) {
            mem::forget(nested);
        }
        BROKEN
    })
}

fn discard_on_panic<T>(
    context: &Context,
    affected: Option<u64>,
    action: impl FnOnce() -> Result<T, i32>,
) -> Result<T, i32> {
    match caught(action) {
        Ok(Err(BROKEN)) | Err(_) => {
            context.lock().fence(affected);
            Err(BROKEN)
        }
        Ok(result) => result,
    }
}

fn context(raw: *mut c_void) -> Result<&'static Context, i32> {
    let expected = CONTEXT.get().ok_or(INVALID)?;
    if !ptr::eq(raw.cast_const(), ptr::from_ref(expected).cast()) {
        return Err(INVALID);
    }
    if expected.owner != std::process::id() {
        return Err(BROKEN);
    }
    Ok(expected)
}

fn locked<T>(
    context: &Context,
    affected: Option<u64>,
    action: impl FnOnce(&mut State) -> Result<T, i32>,
) -> Result<T, i32> {
    let mut state = context.lock();
    match caught(|| action(&mut state)) {
        Ok(Err(BROKEN)) | Err(_) => {
            state.fence(affected);
            Err(BROKEN)
        }
        Ok(result) => result,
    }
}

fn publish(
    context: &Context,
    lane_id: u64,
    intent: Intent,
    blocks: &[Block],
) -> Result<(i32, usize), i32> {
    let Some(first) = blocks.first() else {
        let state = context.lock();
        if state.fenced
            || state.lanes.get(&lane_id).is_some_and(|lane| {
                state
                    .sessions
                    .get(&lane.session_id)
                    .is_some_and(|session| session.fenced)
            })
        {
            return Err(BROKEN);
        }
        intent.validate()?;
        return Ok((OK, 0));
    };
    if first.lease.lane_id != lane_id {
        return Err(INVALID);
    }
    first.extent()?;
    let session_id = first.lease.session_id;
    let affected = Some(session_id);
    discard_on_panic(context, affected, || {
        let Some((work, prefix, mut epoch)) = locked(context, affected, |state| {
            if state.fenced
                || state
                    .sessions
                    .get(&session_id)
                    .is_some_and(|value| value.fenced)
            {
                return Err(BROKEN);
            }
            intent.validate()?;
            intent.check_order(first)?;
            let lane = state.lane_for_publish(lane_id, session_id)?;
            if lane.is_some_and(|lane| lane.consumer_id != intent.consumer_id) {
                return Err(INVALID);
            }
            let prefix = blocks.len().min(lane.map_or(CAPACITY, Lane::space));
            let epoch = lane.and_then(|lane| lane.epoch);
            if prefix == 0 {
                // Even an unconsumed first block must address the current epoch.
                Epoch::advance(epoch, *first)?;
                return Ok(None);
            }
            let session = state.sessions.entry(session_id).or_default();
            if session.preparing {
                return Ok(None);
            }
            session.preparing = true;
            Ok(Some((
                Preparation {
                    context,
                    session: session_id,
                },
                prefix,
                epoch,
            )))
        })?
        else {
            return Ok((AGAIN, 0));
        };
        let mut pending = Vec::with_capacity(prefix);
        for &block in &blocks[..prefix] {
            if block.lease.session_id != session_id || block.lease.lane_id != lane_id {
                return Err(INVALID);
            }
            let extent = block.extent()?;
            epoch = Some(Epoch::advance(epoch, block)?);
            pending.push(Payload::prepare(block, extent)?);
            fault(Fault::Publish);
        }
        let count = locked(context, affected, |state| {
            if state
                .sessions
                .get(&session_id)
                .is_none_or(|value| value.fenced)
            {
                return Err(BROKEN);
            }
            let lane = state.lane_for_publish(lane_id, session_id)?;
            let new = lane
                .is_none()
                .then(|| Lane::new(session_id, intent.consumer_id));
            if new.is_some() {
                state.lanes.try_reserve(1).map_err(|_| BROKEN)?;
            }
            intent.check_deadline()?;
            // Prepared storage and validated counts make the remainder infallible.
            let lane = match new {
                Some(lane) => state.lanes.entry(lane_id).insert_entry(lane).into_mut(),
                None => state.lanes.get_mut(&lane_id).ok_or(BROKEN)?,
            };
            lane.epoch = epoch;
            lane.pending.extend(pending.drain(..));
            Ok(prefix)
        })?;
        drop(work);
        Ok((if count == blocks.len() { OK } else { AGAIN }, count))
    })
}

fn take(
    state: &mut State,
    lane_id: u64,
    intent: Intent,
    delivery: u32,
    out: *mut Block,
    capacity: usize,
) -> Result<(i32, usize), i32> {
    if state.fenced {
        return Err(BROKEN);
    }
    let lane = state.lanes.get_mut(&lane_id);
    if let Some(lane) = &lane {
        if state
            .sessions
            .get(&lane.session_id)
            .is_some_and(|session| session.fenced)
        {
            return Err(BROKEN);
        }
        if intent.consumer_id != lane.consumer_id {
            return Err(INVALID);
        }
    }
    intent.validate()?;
    if !matches!(delivery, ORDERED | LATEST) {
        return Err(INVALID);
    }
    if capacity == 0 {
        return Ok((OK, 0));
    }
    let Some(lane) = lane else {
        return Ok((AGAIN, 0));
    };
    let Some(head) = lane.pending.front() else {
        return Ok((AGAIN, 0));
    };
    intent.check_order(&head.block)?;
    let (skip, count) = match head.block.kind {
        END if lane.leased != 0 => return Ok((AGAIN, 0)),
        END => (0, 1),
        FRAME if delivery == LATEST => (
            lane.pending
                .iter()
                .take_while(|item| item.block.kind == FRAME)
                .count()
                - 1,
            1,
        ),
        _ => (
            0,
            lane.pending
                .iter()
                .take(capacity)
                .take_while(|item| item.block.kind != END)
                .count(),
        ),
    };
    let session = state.sessions.get_mut(&lane.session_id).ok_or(BROKEN)?;
    session.live.try_reserve(count).map_err(|_| BROKEN)?;
    session.issued.try_reserve(count).map_err(|_| BROKEN)?;
    let mut leases = [Lease::default(); CAPACITY];
    for (index, item) in lane.pending.iter().skip(skip).take(count).enumerate() {
        let mut lease = item.block.lease;
        let series = lease.series();
        let prior = leases[..index]
            .iter()
            .filter(|other| other.series() == series)
            .count() as u64;
        lease.reference = session
            .issued
            .get(&series)
            .copied()
            .unwrap_or(0)
            .checked_add(prior + 1)
            .ok_or(BROKEN)?;
        leases[index] = lease;
    }
    fault(Fault::Borrow);
    intent.check_deadline()?;
    // The queue is pinned under the state lock. All capacity was reserved and
    // ordinals computed before transferring custody or touching the array.
    lane.pending.drain(..skip);
    for (index, (mut item, lease)) in lane.pending.drain(..count).zip(leases).enumerate() {
        item.block.lease = lease;
        if index == 0 {
            item.block.skipped = skip as u64;
        }
        let block = item.block;
        session.issued.insert(lease.series(), lease.reference);
        session.live.insert(lease, item);
        unsafe { out.add(index).write(block) };
    }
    lane.leased += count;
    Ok((OK, count))
}

fn entry(action: impl FnOnce() -> Result<i32, i32>) -> i32 {
    caught(action)
        .and_then(|result| result)
        .unwrap_or_else(|status| status)
}

unsafe extern "C" fn publish_batch(
    raw: *mut c_void,
    lane: u64,
    intent: *const Intent,
    blocks: *const Block,
    count: u32,
    consumed: *mut u32,
) -> i32 {
    entry(|| {
        if consumed.is_null() {
            return Err(INVALID);
        }
        unsafe { consumed.write(0) };
        let context = context(raw)?;
        let intent = unsafe { intent.as_ref() }.ok_or(INVALID)?;
        let blocks = if count == 0 {
            &[]
        } else if blocks.is_null() {
            return Err(INVALID);
        } else {
            unsafe { slice::from_raw_parts(blocks, count as usize) }
        };
        let (result, count) = publish(context, lane, *intent, blocks)?;
        unsafe { consumed.write(count as u32) };
        Ok(result)
    })
}

unsafe extern "C" fn borrow_batch(
    raw: *mut c_void,
    lane: u64,
    intent: *const Intent,
    delivery: u32,
    out: *mut Block,
    capacity: u32,
    produced: *mut u32,
) -> i32 {
    entry(|| {
        if produced.is_null() {
            return Err(INVALID);
        }
        unsafe { produced.write(0) };
        let context = context(raw)?;
        let intent = unsafe { intent.as_ref() }.ok_or(INVALID)?;
        if capacity != 0 && out.is_null() {
            return Err(INVALID);
        }
        let (result, count) = {
            let mut state = context.lock();
            let id = state.lanes.get(&lane).map(|lane| lane.session_id);
            match caught(|| take(&mut state, lane, *intent, delivery, out, capacity as usize)) {
                Ok(Err(BROKEN)) | Err(_) => {
                    state.fence(id);
                    return Err(BROKEN);
                }
                Ok(result) => result?,
            }
        };
        unsafe { produced.write(count as u32) };
        Ok(result)
    })
}

unsafe extern "C" fn cpu_view(raw: *mut c_void, lease: *const Lease, out: *mut View) -> i32 {
    entry(|| {
        if out.is_null() {
            return Err(INVALID);
        }
        unsafe { out.write(View::EMPTY) };
        let context = context(raw)?;
        let lease = unsafe { lease.as_ref() }.ok_or(INVALID)?;
        locked(context, Some(lease.session_id), |state| {
            let session = state.sessions.get(&lease.session_id);
            if state.fenced || session.is_some_and(|session| session.fenced) {
                return Err(BROKEN);
            }
            fault(Fault::View);
            let payload = session
                .and_then(|session| session.live.get(lease))
                .ok_or(STALE)?;
            unsafe { out.write(payload.view()?) };
            Ok(OK)
        })
    })
}

unsafe extern "C" fn release(raw: *mut c_void, lease: *const Lease) -> i32 {
    entry(|| {
        let context = context(raw)?;
        let lease = unsafe { lease.as_ref() }.ok_or(INVALID)?;
        locked(context, Some(lease.session_id), |state| {
            let session = state.sessions.get_mut(&lease.session_id).ok_or(STALE)?;
            if !session.live.contains_key(lease) {
                let through = session.issued.get(&lease.series()).copied().unwrap_or(0);
                return Err(if (1..=through).contains(&lease.reference) {
                    ALREADY_RELEASED
                } else {
                    STALE
                });
            }
            let lane = state.lanes.get_mut(&lease.lane_id).ok_or(BROKEN)?;
            let remaining = lane.leased.checked_sub(1).ok_or(BROKEN)?;
            fault(Fault::Release);
            session.live.remove(lease);
            lane.leased = remaining;
            Ok(OK)
        })
    })
}

unsafe extern "C" fn break_and_drain(raw: *mut c_void, session_id: u64) -> i32 {
    entry(|| {
        let context = context(raw)?;
        locked(context, Some(session_id), |state| {
            // Record the fence even if the first publication is still arriving.
            state.fence(Some(session_id));
            fault(Fault::Drain);
            let session = state.sessions.get(&session_id).ok_or(BROKEN)?;
            Ok(if session.preparing || !session.live.is_empty() {
                AGAIN
            } else {
                OK
            })
        })
    })
}

/// # Safety
/// With matching layout sizes a non-null output is writable and aligned.
#[no_mangle]
pub unsafe extern "C" fn vp_bulk_get_api_v1(
    version: u32,
    api_size: u32,
    intent_size: u32,
    lease_size: u32,
    block_size: u32,
    view_size: u32,
    out: *mut Api,
) -> i32 {
    entry(|| {
        if [
            version,
            api_size,
            intent_size,
            lease_size,
            block_size,
            view_size,
        ] != [
            ABI,
            size_of::<Api>() as u32,
            size_of::<Intent>() as u32,
            size_of::<Lease>() as u32,
            size_of::<Block>() as u32,
            size_of::<View>() as u32,
        ] {
            return Err(ABI_MISMATCH);
        }
        if out.is_null() {
            return Err(INVALID);
        }
        if let Err(status) = caught(|| fault(Fault::Bootstrap)) {
            if let Some(context) = CONTEXT.get().filter(|c| c.owner == std::process::id()) {
                context.lock().fence(None);
            }
            return Err(status);
        }
        let context = CONTEXT.get_or_init(|| Context {
            owner: std::process::id(),
            state: Mutex::new(State::default()),
        });
        if context.owner != std::process::id() {
            return Err(BROKEN);
        }
        unsafe {
            out.write(Api {
                abi_version: ABI,
                struct_size: api_size,
                context: ptr::from_ref(context).cast_mut().cast(),
                publish_batch,
                borrow_batch,
                cpu_view,
                release,
                break_and_drain,
            });
        }
        Ok(OK)
    })
}

#[derive(Clone, Copy, PartialEq)]
enum Fault {
    Bootstrap,
    Publish,
    Borrow,
    View,
    Release,
    Drain,
}

#[cfg(test)]
thread_local! {
    static INJECT_FAULT: std::cell::Cell<Option<Fault>> = const { std::cell::Cell::new(None) };
    static PUBLISH_HOOK: std::cell::RefCell<Option<Box<dyn FnOnce()>>> = const { std::cell::RefCell::new(None) };
}

fn fault(point: Fault) {
    #[cfg(test)]
    if point == Fault::Publish {
        PUBLISH_HOOK.with(|hook| {
            if let Some(ready) = hook.take() {
                ready();
            }
        });
    }
    #[cfg(test)]
    INJECT_FAULT.with(|fault| {
        if fault.get() == Some(point) {
            fault.set(None);
            panic!("synthetic ABI panic");
        }
    });
    #[cfg(not(test))]
    let _ = point;
}

#[cfg(test)]
#[path = "../tests/rust/bulk.rs"]
mod tests;
