//! Integer-only port of eval/v2x/gm_bands_hv.py (v2.1).
//! Motion proposals do not constitute claims: the common exact verifier and
//! changed-only public tile consensus are applied to every candidate.
use std::cmp::Ordering;
use std::collections::BTreeSet;

const TILE: usize = 16;
const TILE_CAP: u32 = 128;
const MIN_SUPPORT: u64 = 512;
const PROJECTION_SMOOTH: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct Shift { dx: i32, dy: i32 }

#[derive(Clone, Copy)]
struct Rect { y0: usize, y1: usize, x0: usize, x1: usize }
impl Rect {
    fn h(self) -> usize { self.y1 - self.y0 }
    fn w(self) -> usize { self.x1 - self.x0 }
}
struct Prepared<'a> {
    prev: &'a [u8], cur: &'a [u8], h: usize, w: usize,
    allowed: Vec<bool>, band: Rect,
}
#[derive(Clone)]
struct Evidence {
    shift: Shift,
    raw: u128, n: u128, score: i64,
    changed_weight: u64, support_weight: u64,
    matched: Vec<bool>,
}
struct Pixels {
    rect: Rect,
    valid: Vec<bool>,
    changed: Vec<bool>,
    info: Vec<bool>,
}
fn at(w: usize, y: usize, x: usize) -> usize { y*w+x }
fn absdiff(a:u8,b:u8)->u128 { a.abs_diff(b) as u128 }
fn min_overlap(len:usize)->usize { 16.max(64.min(len/4)) }
fn ceil_div(n:usize,s:usize)->usize { n/s + usize::from(n%s != 0) }
fn grid(h:usize,w:usize,s:usize)->(usize,usize) {(ceil_div(h,s),ceil_div(w,s))}
fn overlap(b:Rect, sh:Shift)->Option<Rect> {
    let dx=sh.dx as i64; let dy=sh.dy as i64;
    let y0=(b.y0 as i64).max(b.y0 as i64+dy);
    let y1=(b.y1 as i64).min(b.y1 as i64+dy);
    let x0=(b.x0 as i64).max(b.x0 as i64+dx);
    let x1=(b.x1 as i64).min(b.x1 as i64+dx);
    if y0>=y1||x0>=x1 {None} else {
        Some(Rect{y0:y0 as usize,y1:y1 as usize,x0:x0 as usize,x1:x1 as usize})
    }
}
fn informative(frame:&[u8],allowed:&[bool],h:usize,w:usize)->Vec<bool>{
    let mut info=vec![false;h*w];
    for y in 0..h { for x in 0..w {
        let i=at(w,y,x);
        if !allowed[i] {continue}
        if x+1<w {
            let j=i+1;
            if allowed[j]&&frame[i]!=frame[j] {info[i]=true;info[j]=true;}
        }
        if y+1<h {
            let j=i+w;
            if allowed[j]&&frame[i]!=frame[j] {info[i]=true;info[j]=true;}
        }
    }}
    info
}
fn compact_allowed(prev:&[u8],cur:&[u8],h:usize,w:usize)->Vec<bool>{
    let mut first=vec![w;h]; let mut last=vec![0;h];
    for y in 0..h {for x in 0..w {
        if prev[at(w,y,x)]!=cur[at(w,y,x)] {
            first[y]=first[y].min(x);last[y]=x+1;
        }
    }}
    let edge=64.min(h/4);
    let broad:Vec<usize>=(edge..h-edge)
        .filter(|&y|first[y]<last[y]&&last[y]-first[y]>w/8).collect();
    let mut allowed=vec![true;h*w];
    if broad.is_empty(){return allowed}
    for y in 0..h {
        if first[y]==w||last[y]-first[y]>w/8 {continue}
        let separated=(y<edge && y.saturating_add(16)<broad[0])
            ||(y>=h-edge && y>=16 && y-16>*broad.last().unwrap());
        if !separated {continue}
        let x0=first[y];let x1=last[y];
        let mut flat:Option<u8>=None;let mut qualifies=false;let mut ok=true;
        for x in 0..w {if x>=x0&&x<x1 {continue}
            let i=at(w,y,x);
            if prev[i]!=cur[i] {ok=false;break}
            if let Some(v)=flat {if v!=prev[i] {ok=false;break}}
            else {flat=Some(prev[i]);}
            qualifies=true;
        }
        if ok&&qualifies {
            for x in x0..x1 {
                let i=at(w,y,x);if prev[i]!=cur[i] {allowed[i]=false}
            }
        }
    }
    allowed
}
fn prepare<'a>(prev:&'a [u8],cur:&'a [u8],h:usize,w:usize)->Option<Prepared<'a>>{
    let allowed=compact_allowed(prev,cur,h,w);
    let pi=informative(prev,&allowed,h,w);
    let ci=informative(cur,&allowed,h,w);
    let mut y0=h;let mut y1=0;let mut x0=w;let mut x1=0;
    for y in 0..h {for x in 0..w{
        let i=at(w,y,x);
        if allowed[i]&&(prev[i]!=cur[i]||pi[i]||ci[i]){
            y0=y0.min(y);y1=y+1;x0=x0.min(x);x1=x1.max(x+1);
        }
    }}
    if y0==h {return None}
    Some(Prepared{prev,cur,h,w,allowed,band:Rect{y0,y1,x0,x1}})
}
fn pixels(st:&Prepared,sh:Shift)->Option<Pixels>{
    let r=overlap(st.band,sh)?;
    if r.h()<min_overlap(st.band.h())||r.w()<min_overlap(st.band.w()){return None}
    let mut valid=vec![false;r.h()*r.w()];
    let mut c=vec![0;r.h()*r.w()];
    let mut p=vec![0;r.h()*r.w()];
    for y in 0..r.h(){for x in 0..r.w(){
        let cy=r.y0+y;let cx=r.x0+x;
        let py=(cy as i64-sh.dy as i64) as usize;
        let px=(cx as i64-sh.dx as i64) as usize;
        let i=at(st.w,cy,cx);let j=at(st.w,py,px);let k=at(r.w(),y,x);
        valid[k]=st.allowed[i]&&st.allowed[j];
        c[k]=st.cur[i];p[k]=st.prev[j];
    }}
    let ci=informative(&c,&valid,r.h(),r.w());
    let pi=informative(&p,&valid,r.h(),r.w());
    let mut info=vec![false;valid.len()];let mut changed=vec![false;valid.len()];
    for y in 0..r.h(){for x in 0..r.w(){
        let k=at(r.w(),y,x);let i=at(st.w,r.y0+y,r.x0+x);
        info[k]=ci[k]||pi[k];
        changed[k]=info[k]&&c[k]!=st.prev[i];
    }}
    Some(Pixels{rect:r,valid,changed,info})
}
fn verify(st:&Prepared,sh:Shift)->Option<Evidence>{
    let pix=pixels(st,sh)?;
    let (gh,gw)=grid(st.h,st.w,TILE);
    let len=gh.checked_mul(gw)?;
    let mut weights=vec![0u32;len];
    let mut mismatches=vec![0u32;len];
    let mut n=0u128;let mut raw=0u128;
    let r=pix.rect;
    for y in 0..r.h(){for x in 0..r.w(){
        let k=at(r.w(),y,x);
        let cy=r.y0+y;let cx=r.x0+x;
        let i=at(st.w,cy,cx);
        let py=(cy as i64-sh.dy as i64) as usize;
        let px=(cx as i64-sh.dx as i64) as usize;
        let j=at(st.w,py,px);
        let t=at(gw,cy/TILE,cx/TILE);
        if pix.info[k]{
            n+=1;raw+=absdiff(st.cur[i],st.prev[j]);
            if pix.changed[k]{weights[t]+=1;}
        }
        if pix.valid[k]&&st.cur[i]!=st.prev[j] {mismatches[t]+=1;}
    }}
    if n==0 {return None}
    let mut changed_weight=0u64;let mut support_weight=0u64;
    let mut matched=vec![false;len];
    for t in 0..len{
        let weight=weights[t].min(TILE_CAP) as u64;
        changed_weight+=weight;
        if weight!=0&&mismatches[t]==0{matched[t]=true;support_weight+=weight}
    }
    Some(Evidence{shift:sh,raw,n,score:((raw+n-1)/n) as i64,changed_weight,support_weight,matched})
}
// Exact positive rational ordering without cross-product overflow.
fn fraction_cmp(mut an:u128,mut ad:u128,mut bn:u128,mut bd:u128)->Ordering{
    let mut inverse=false;
    loop{
        let a=an/ad;let b=bn/bd;
        if a!=b {let cmp=a.cmp(&b);return if inverse{cmp.reverse()}else{cmp}}
        let ar=an%ad;let br=bn%bd;
        if ar==0||br==0{
            let cmp=match (ar==0,br==0){(true,true)=>Ordering::Equal,(true,false)=>Ordering::Less,_=>Ordering::Greater};
            return if inverse{cmp.reverse()}else{cmp}
        }
        an=ad;ad=ar;bn=bd;bd=br;inverse=!inverse;
    }
}
fn cmp_evidence(a:&Evidence,b:&Evidence)->Ordering{
    fraction_cmp(a.raw,a.n,b.raw,b.n)
        .then_with(||((a.shift.dx as i64).abs()+(a.shift.dy as i64).abs(),a.shift.dx,a.shift.dy)
            .cmp(&((b.shift.dx as i64).abs()+(b.shift.dy as i64).abs(),b.shift.dx,b.shift.dy)))
}
fn unique(best:&Evidence,others:&[Evidence])->bool{
    for other in others {
        if ((best.shift.dx as i64-other.shift.dx as i64).abs())
            .max((best.shift.dy as i64-other.shift.dy as i64).abs())<4 {continue}
        let cmp=fraction_cmp(other.raw,other.n,best.raw,best.n);
        if cmp==Ordering::Equal {return false}
        if cmp==Ordering::Less {return false}
        if best.raw!=0 {
            let lhs=other.raw.checked_mul(best.n);
            let rhs=best.raw.checked_mul(other.n)
                .and_then(|v|best.n.checked_mul(other.n).and_then(|gap|v.checked_add(gap)));
            if match (lhs,rhs){(Some(l),Some(r))=>l<r,_=>true}{return false}
        }
    }
    true
}
fn runner_support(st:&Prepared,primary:&Evidence,main:&Pixels,runner:&Evidence)->u64{
    let Some(r)=overlap(st.band,runner.shift) else{return 0};
    let (gh,gw)=grid(st.h,st.w,TILE);
    let mut weight=vec![0u32;gh*gw];let mut mismatch=vec![false;gh*gw];
    for y in r.y0..r.y1 {for x in r.x0..r.x1 {
        if y<main.rect.y0||y>=main.rect.y1||x<main.rect.x0||x>=main.rect.x1{continue}
        let pidx=at(main.rect.w(),y-main.rect.y0,x-main.rect.x0);
        if !main.valid[pidx]{continue}
        let py=(y as i64-runner.shift.dy as i64) as usize;
        let px=(x as i64-runner.shift.dx as i64) as usize;
        let src=at(st.w,py,px);
        if !st.allowed[src]{continue}
        let tile=at(gw,y/TILE,x/TILE);
        if main.changed[pidx]{weight[tile]+=1;}
        if st.cur[at(st.w,y,x)]!=st.prev[src]{mismatch[tile]=true;}
    }}
    let mut support=0;
    for t in 0..gh*gw{if !mismatch[t]{support+=weight[t].min(TILE_CAP) as u64}}
    let _=primary;
    support
}
fn sparse_exact(st:&Prepared,best:&Evidence,main:&Pixels,others:&[Evidence])->bool{
    if best.raw!=0||best.support_weight!=best.changed_weight||best.changed_weight==0
        ||others.iter().any(|e|e.raw==0){return false}
    let r=main.rect;
    let mut c=vec![0u8;r.h()*r.w()];let mut p=vec![0u8;r.h()*r.w()];
    for y in 0..r.h(){for x in 0..r.w(){
        let k=at(r.w(),y,x);let cy=r.y0+y;let cx=r.x0+x;
        c[k]=st.cur[at(st.w,cy,cx)];
        let py=(cy as i64-best.shift.dy as i64) as usize;
        let px=(cx as i64-best.shift.dx as i64) as usize;
        p[k]=st.prev[at(st.w,py,px)];
        if main.valid[k]&&c[k]!=p[k]{return false}
    }}
    let ci=informative(&c,&main.valid,r.h(),r.w());
    let pi=informative(&p,&main.valid,r.h(),r.w());
    let (gh,gw)=grid(st.h,st.w,TILE);
    let mut weights=vec![0u32;gh*gw];
    for y in 0..r.h(){for x in 0..r.w(){
        let k=at(r.w(),y,x);
        if !(ci[k]||pi[k])||main.changed[k]{continue}
        let adj=(y>0&&main.changed[k-r.w()])
            ||(y+1<r.h()&&main.changed[k+r.w()])
            ||(x>0&&main.changed[k-1])
            ||(x+1<r.w()&&main.changed[k+1]);
        if adj {weights[at(gw,(r.y0+y)/TILE,(r.x0+x)/TILE)]+=1}
    }}
    let mut support=0u64;
    for i in 0..weights.len(){if best.matched[i]{support+=weights[i].min(TILE_CAP) as u64}}
    support>=MIN_SUPPORT
}
fn fixed_chrome_exact(st:&Prepared,best:&Evidence)->bool{
    let sh=best.shift;
    if (sh.dx==0)==(sh.dy==0){return false}
    let mut r=Rect{y0:0,y1:st.h,x0:0,x1:st.w};
    if sh.dx==0{
        let active:Vec<usize>=(0..st.h).filter(|&y|(0..st.w).any(|x|
            st.prev[at(st.w,y,x)]!=st.cur[at(st.w,y,x)])).collect();
        if active.is_empty()||active[0]==0||*active.last().unwrap()==st.h-1{return false}
        r.y0=active[0];r.y1=active[active.len()-1]+1;
    } else {
        let active:Vec<usize>=(0..st.w).filter(|&x|(0..st.h).any(|y|
            st.prev[at(st.w,y,x)]!=st.cur[at(st.w,y,x)])).collect();
        if active.is_empty()||active[0]==0||*active.last().unwrap()==st.w-1{return false}
        r.x0=active[0];r.x1=active[active.len()-1]+1;
    }
    let Some(o)=overlap(r,sh)else{return false};
    if o.h()<min_overlap(r.h())||o.w()<min_overlap(r.w()){return false}
    for y in o.y0..o.y1 {for x in o.x0..o.x1{
        let py=(y as i64-sh.dy as i64) as usize;
        let px=(x as i64-sh.dx as i64) as usize;
        if st.cur[at(st.w,y,x)]!=st.prev[at(st.w,py,px)]{return false}
    }}
    true
}
fn rank_projection(cur:&[u128],prev:&[u128],max_d:i32,minimum:usize)->Vec<(u128,i64,i64)>{
    let len=cur.len();let range=(max_d as usize).min(len.saturating_sub(1)) as i64;
    let mut records=Vec::new();
    for shift in -range..=range{
        let lo=0i64.max(shift) as usize;
        let hi=(len as i64).min(len as i64+shift) as usize;
        if hi-lo<minimum{continue}
        let mut raw=0u128;
        for i in lo..hi{raw+=cur[i].abs_diff(prev[(i as i64-shift) as usize]);}
        let score=raw.checked_mul(len as u128).map(|v|v/(hi-lo) as u128).unwrap_or(u128::MAX);
        records.push((score,shift.abs(),shift));
    }
    records.sort();
    records
}
fn axis_candidates(cur:&[u128],prev:&[u128],max_d:i32,minimum:usize,keep:usize)->BTreeSet<i32>{
    let raw=rank_projection(cur,prev,max_d,minimum);
    let mut selected:BTreeSet<i32>=raw.iter().take(keep).map(|r|r.2 as i32).collect();
    selected.insert(0);
    if cur.len()>=PROJECTION_SMOOTH{
        let smooth=|a:&[u128]|->Vec<u128>{
            let mut result=Vec::with_capacity(a.len()-7);
            let mut sum:u128=0;
            for &v in &a[..8]{sum+=v}
            result.push(sum);
            for i in 8..a.len(){sum=sum-a[i-8]+a[i];result.push(sum)}
            result
        };
        let cs=smooth(cur);let ps=smooth(prev);
        let rank=rank_projection(&cs,&ps,max_d,1.max(minimum.saturating_sub(7)));
        for center in rank.iter().filter(|r|r.2%8==0).take(8){
            if let Some(one)=raw.iter().find(|r|(r.2-center.2).abs()<8) {
                selected.insert(one.2 as i32);
            }
        }
    }
    selected
}
fn projection_pairs(st:&Prepared,r:Rect,max_d:i32,keep:usize)->BTreeSet<Shift>{
    let mut pairs=BTreeSet::new();
    if r.h()<16||r.w()<16{return pairs}
    let mut pr=vec![0u128;r.h()];let mut cr=vec![0u128;r.h()];
    for y in 0..r.h(){for x in 0..r.w(){
        pr[y]+=st.prev[at(st.w,r.y0+y,r.x0+x)] as u128;
        cr[y]+=st.cur[at(st.w,r.y0+y,r.x0+x)] as u128;
    }}
    for dy in axis_candidates(&cr,&pr,max_d,min_overlap(r.h()),keep){
        let lo=0i64.max(dy as i64) as usize;
        let hi=(r.h() as i64).min(r.h() as i64+dy as i64) as usize;
        if hi-lo<min_overlap(r.h()){continue}
        let mut pc=vec![0u128;r.w()];let mut cc=vec![0u128;r.w()];
        for y in lo..hi{for x in 0..r.w(){
            cc[x]+=st.cur[at(st.w,r.y0+y,r.x0+x)] as u128;
            pc[x]+=st.prev[at(st.w,r.y0+(y as i64-dy as i64) as usize,r.x0+x)] as u128;
        }}
        for dx in axis_candidates(&cc,&pc,max_d,min_overlap(r.w()),keep.min(4)){
            pairs.insert(Shift{dx,dy});
        }
    }
    pairs
}
fn candidates(st:&Prepared,max_d:i32)->BTreeSet<Shift>{
    let mut pairs=projection_pairs(st,st.band,max_d,8);
    let mut runs:Vec<(usize,usize,usize,usize,usize)>=Vec::new();
    let mut y=0usize;
    while y<st.h{
        let changed_row=|yy:usize|->bool{(0..st.w).any(|x|{
            let i=at(st.w,yy,x);st.allowed[i]&&st.prev[i]!=st.cur[i]
        })};
        if !changed_row(y){y+=1;continue}
        let ya=y;
        let mut xa=st.w;let mut xb=0;let mut count=0usize;
        while y<st.h&&changed_row(y){
            for x in 0..st.w{
                let i=at(st.w,y,x);
                if st.allowed[i]&&st.prev[i]!=st.cur[i]{
                    xa=xa.min(x);xb=xb.max(x+1);count+=1;
                }
            }
            y+=1;
        }
        runs.push((count,ya,y,xa,xb));
    }
    runs.sort_by(|a,b|b.0.cmp(&a.0).then_with(||a.1.cmp(&b.1))
        .then_with(||a.2.cmp(&b.2)).then_with(||a.3.cmp(&b.3)).then_with(||a.4.cmp(&b.4)));
    for &(_,y0,y1,x0,x1) in runs.iter().take(8){
        let r=Rect{y0,y1,x0,x1};
        if (r.y0,r.y1,r.x0,r.x1)!=(st.band.y0,st.band.y1,st.band.x0,st.band.x1) {
            pairs.extend(projection_pairs(st,r,max_d,3));
        }
    }
    pairs.insert(Shift{dx:0,dy:0});pairs
}
fn decide(st:&Prepared,max_d:i32)->Option<(i32,i32,i64)>{
    let mut evidence:Vec<Evidence>=Vec::new();
    for sh in candidates(st,max_d){
        if (sh.dx as i64).abs()>max_d as i64||(sh.dy as i64).abs()>max_d as i64{continue}
        if let Some(ev)=verify(st,sh){evidence.push(ev)}
    }
    evidence.sort_by(cmp_evidence);
    let best=evidence.first()?.clone();
    if (best.shift.dx==0 && best.shift.dy==0) || !unique(&best,&evidence[1..]) {return None}
    let (_,gw)=grid(st.h,st.w,TILE);
    let mut tile_count=0usize;let mut rows=BTreeSet::new();let mut cols=BTreeSet::new();
    for (i,&v) in best.matched.iter().enumerate(){if v {
        tile_count+=1;rows.insert(i/gw);cols.insert(i%gw);
    }}
    if tile_count<4||rows.len()<2||cols.len()<2||best.changed_weight==0
        ||best.support_weight*1000<600*best.changed_weight {return None}
    let main=pixels(st,best.shift)?;
    let sparse=best.support_weight<MIN_SUPPORT;
    if sparse&&!sparse_exact(st,&best,&main,&evidence[1..]){return None}
    for runner in evidence.iter().skip(1) {
        if runner.shift.dx==0 && runner.shift.dy==0 {continue}
        let support=runner_support(st,&best,&main,runner);
        if support*1000>=250*best.changed_weight||best.support_weight<3*support{return None}
    }
    let mut score=best.score;
    if score>0&&fixed_chrome_exact(st,&best){score=0}
    Some((best.shift.dx,best.shift.dy,score))
}
/// Returns a v2.1 informative motion tuple, or None for ambiguity/inadequate
/// distributed evidence. k_top is not used by this estimator.
pub fn estimate_global_motion(prev:&[u8],cur:&[u8],h:usize,w:usize,max_d:i32)->Option<(i32,i32,i64)>{
    if h==0||w==0||max_d<0||h.checked_mul(w)!=Some(prev.len())||prev.len()!=cur.len(){return None}
    let flat=|a:&[u8]|a.iter().all(|&v|v==a[0]);
    if flat(prev)&&flat(cur){return None}
    if prev==cur {return Some((0,0,0))}
    let st=prepare(prev,cur,h,w)?;
    decide(&st,max_d)
}
/// Whole REAL destination tile must have an in-bounds equal translated source
/// and at least one byte changed at the same in-place destination.
pub fn consensus_tiles(prev:&[u8],cur:&[u8],h:usize,w:usize,s:usize,dx:i32,dy:i32)->Vec<u8>{
    if s==0||h.checked_mul(w)!=Some(prev.len())||prev.len()!=cur.len(){return Vec::new()}
    let (gh,gw)=grid(h,w,s);
    let mut result=vec![0u8;gh*gw];
    for ty in 0..gh {for tx in 0..gw{
        let y0=ty*s;let x0=tx*s;
        let y1=y0.saturating_add(s).min(h);let x1=x0.saturating_add(s).min(w);
        let mut equal=true;let mut changed=false;
        'pixels:for y in y0..y1{for x in x0..x1{
            let py=y as i64-dy as i64;let px=x as i64-dx as i64;
            if py<0||px<0||py>=h as i64||px>=w as i64{equal=false;break 'pixels}
            let i=at(w,y,x);let j=at(w,py as usize,px as usize);
            if cur[i]!=prev[j]{equal=false;break 'pixels}
            if cur[i]!=prev[i]{changed=true}
        }}
        if equal&&changed{result[at(gw,ty,tx)]=1}
    }}
    result
}
