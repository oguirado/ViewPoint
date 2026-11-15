//! Bare little-endian GM v2 stdin/stdout protocol.
//! No magic, length framing, network, or recover-after-malformed-input behavior.
use std::io::{self, Read, Write};
use vqa_k_gm_v2::{consensus_tiles, estimate_global_motion};

fn invalid(msg: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg)
}
fn word<R: Read>(reader: &mut R) -> io::Result<[u8;4]> {
    let mut b=[0u8;4];
    reader.read_exact(&mut b)?;
    Ok(b)
}
fn u32_le<R: Read>(reader: &mut R)->io::Result<u32>{
    Ok(u32::from_le_bytes(word(reader)?))
}
fn i32_le<R: Read>(reader: &mut R)->io::Result<i32>{
    Ok(i32::from_le_bytes(word(reader)?))
}
fn read_op<R:Read>(reader:&mut R)->io::Result<Option<i32>>{
    let mut b=[0u8;4];
    loop {
        match reader.read(&mut b[..1]) {
            Ok(0)=>return Ok(None), // Clean EOF ONLY between requests.
            Ok(_)=>break,
            Err(e) if e.kind()==io::ErrorKind::Interrupted=>continue,
            Err(e)=>return Err(e),
        }
    }
    reader.read_exact(&mut b[1..])?; // Partial opcode is an error.
    Ok(Some(i32::from_le_bytes(b)))
}
fn dimensions(h:u32,w:u32,s:u32)->io::Result<(usize,usize,usize,usize,usize)>{
    if h==0||w==0||s==0{return Err(invalid("zero dimensions or tile size"))}
    let (h,w,s)=(h as usize,w as usize,s as usize);
    let len=h.checked_mul(w).ok_or_else(||invalid("frame length overflow"))?;
    let gh=h/s+usize::from(h%s!=0);
    let gw=w/s+usize::from(w%s!=0);
    gh.checked_mul(gw).ok_or_else(||invalid("grid length overflow"))?;
    Ok((h,w,s,gh,gw))
}
fn frames<R:Read>(reader:&mut R,len:usize)->io::Result<(Vec<u8>,Vec<u8>)>{
    let mut previous=Vec::new();
    previous.try_reserve_exact(len).map_err(|_|invalid("frame allocation failed"))?;
    previous.resize(len,0);
    let mut current=Vec::new();
    current.try_reserve_exact(len).map_err(|_|invalid("frame allocation failed"))?;
    current.resize(len,0);
    reader.read_exact(&mut previous)?;
    reader.read_exact(&mut current)?;
    Ok((previous,current))
}
fn compute<R:Read,W:Write>(input:&mut R,output:&mut W)->io::Result<()>{
    let h=u32_le(input)?;let w=u32_le(input)?;
    let max_d=i32_le(input)?;let _k_top=u32_le(input)?;
    let s=u32_le(input)?;
    let (h,w,s,gh,gw)=dimensions(h,w,s)?;
    if max_d<0{return Err(invalid("negative max_d"))}
    let len=h.checked_mul(w).ok_or_else(||invalid("frame length overflow"))?;
    let (prev,cur)=frames(input,len)?;
    let (has,dx,dy,score)=match estimate_global_motion(&prev,&cur,h,w,max_d){
        Some((dx,dy,score))=>(1u8,dx,dy,score),
        None=>(0u8,0,0,0),
    };
    let mask=consensus_tiles(&prev,&cur,h,w,s,dx,dy);
    if mask.len()!=gh*gw{return Err(invalid("invalid consensus length"))}
    output.write_all(&0i32.to_le_bytes())?;
    output.write_all(&[has])?;
    output.write_all(&dx.to_le_bytes())?;
    output.write_all(&dy.to_le_bytes())?;
    output.write_all(&score.to_le_bytes())?;
    output.write_all(&(gh as u32).to_le_bytes())?;
    output.write_all(&(gw as u32).to_le_bytes())?;
    output.write_all(&mask)?;
    output.flush()?;
    Ok(())
}
fn explicit_consensus<R:Read,W:Write>(input:&mut R,output:&mut W)->io::Result<()>{
    let h=u32_le(input)?;let w=u32_le(input)?;let s=u32_le(input)?;
    let dx=i32_le(input)?;let dy=i32_le(input)?;
    let (h,w,s,gh,gw)=dimensions(h,w,s)?;
    let len=h.checked_mul(w).ok_or_else(||invalid("frame length overflow"))?;
    let (prev,cur)=frames(input,len)?;
    let mask=consensus_tiles(&prev,&cur,h,w,s,dx,dy);
    if mask.len()!=gh*gw{return Err(invalid("invalid consensus length"))}
    output.write_all(&0i32.to_le_bytes())?;
    output.write_all(&(gh as u32).to_le_bytes())?;
    output.write_all(&(gw as u32).to_le_bytes())?;
    output.write_all(&mask)?;
    output.flush()?;
    Ok(())
}
fn serve()->io::Result<()>{
    let stdin=io::stdin();let stdout=io::stdout();
    let mut input=stdin.lock();let mut output=stdout.lock();
    loop {
        match read_op(&mut input)? {
            None|Some(0)=>return Ok(()),
            Some(1)=>compute(&mut input,&mut output)?,
            Some(2)=>explicit_consensus(&mut input,&mut output)?,
            Some(_)=>return Err(invalid("unsupported opcode")),
        }
    }
}
fn main(){
    if let Err(err)=serve(){
        eprintln!("vqa_k_gm_v2: {err}");
        std::process::exit(1);
    }
}
