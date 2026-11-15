/* Exact uint8 comparison kernels; no approximate scores or search pruning. */
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <signal.h>
#include <unistd.h>
#include <sys/prctl.h>

int64_t sad(const uint8_t *p, const uint8_t *c, int h, int w, int dx, int dy) {
    int y0=dy>0?dy:0, y1=dy<0?h+dy:h;
    int x0=dx>0?dx:0, x1=dx<0?w+dx:w;
    if(y0>=y1 || x0>=x1) return INT64_C(1)<<60;
    int64_t total=0;
    for(int y=y0;y<y1;y++) {
        const uint8_t *a=c+y*w+x0, *b=p+(y-dy)*w+x0-dx;
        for(int x=0;x<x1-x0;x++) total+=abs((int)a[x]-(int)b[x]);
    }
    return total*h*w/((y1-y0)*(x1-x0));
}

static int same(const uint8_t *p,const uint8_t *c,int h,int w,int x,int y,int dx,int dy) {
    for(int j=0;j<8;j++) {
        int sy=y+j-dy; if(sy<0)sy=0; if(sy>=h)sy=h-1;
        for(int i=0;i<8;i++) {
            int sx=x+i-dx; if(sx<0)sx=0; if(sx>=w)sx=w-1;
            if(c[(y+j)*w+x+i]!=p[sy*w+sx])return 0;
        }
    }
    return 1;
}

void subtiles(const uint8_t *p,const uint8_t *c,int h,int w,int s,int gw,int gh,
              const int32_t *tx,const int32_t *ty,int n,const int32_t *cand,int nc,int32_t *out) {
    int *bx=malloc((size_t)(3*s/8+2)*(3*s/8+2)*sizeof(int));
    int *by=malloc((size_t)(3*s/8+2)*(3*s/8+2)*sizeof(int));
    if(!bx||!by) abort();
    for(int t=0;t<n;t++) {
        int x0=(tx[t]>0?tx[t]-1:0)*s, y0=(ty[t]>0?ty[t]-1:0)*s;
        int x1=(tx[t]+2<gw?tx[t]+2:gw)*s, y1=(ty[t]+2<gh?ty[t]+2:gh)*s;
        if(x1>w)x1=w;if(y1>h)y1=h;
        int nb=0;
        for(int y=y0;y<y1;y+=8)for(int x=x0;x<x1;x+=8) {
            if(y+8>h||x+8>w)continue;
            if(!same(p,c,h,w,x,y,0,0)){bx[nb]=x;by[nb++]=y;}
        }
        out[3*t]=0;out[3*t+1]=0;out[3*t+2]=0;
        if(!nb)continue;
        for(int k=0;k<nc;k++) {
            int dx=cand[2*k],dy=cand[2*k+1],ok=1;
            for(int b=0;b<nb;b++)if(!same(p,c,h,w,bx[b],by[b],dx,dy)){ok=0;break;}
            if(ok){out[3*t]=dx;out[3*t+1]=dy;out[3*t+2]=nb;break;}
        }
    }
    free(bx);free(by);
}

static void rd(void *p,size_t n){if(fread(p,1,n,stdin)!=n)exit(2);}
static void wr(void *p,size_t n){if(fwrite(p,1,n,stdout)!=n)exit(2);fflush(stdout);}
int main(void){
    prctl(PR_SET_PDEATHSIG,SIGTERM);
    uint8_t *planes[32]={0};int hh[32]={0},ww[32]={0};
    for(;;){
        alarm(120);
        int32_t op; if(fread(&op,4,1,stdin)!=1)break;
        if(op==0){for(int i=0;i<32;i++){free(planes[i]);planes[i]=0;}}
        else if(op==1){
            int32_t a[3];rd(a,sizeof a);int id=a[0],h=a[1],w=a[2];
            if(id<0||id>=32||h<1||w<1) return 3;
            free(planes[id]);planes[id]=malloc((size_t)h*w);if(!planes[id])return 4;
            hh[id]=h;ww[id]=w;rd(planes[id],(size_t)h*w);
        }
        else if(op==2){
            int32_t a[4];rd(a,sizeof a);
            if(a[0]<0||a[0]>=32||a[1]<0||a[1]>=32||!planes[a[0]]||!planes[a[1]])return 5;
            int64_t value=sad(planes[a[0]],planes[a[1]],hh[a[1]],ww[a[1]],a[2],a[3]);
            wr(&value,8);
        }
        else if(op==4){
            int32_t a[3];rd(a,sizeof a);int p=a[0],c=a[1],n=a[2];
            if(p<0||p>=32||c<0||c>=32||n<0||!planes[p]||!planes[c])return 9;
            int32_t *v=malloc((size_t)n*8);int64_t *out=malloc((size_t)n*8);
            if(!v||!out)return 10;
            rd(v,(size_t)n*8);
            for(int i=0;i<n;i++)out[i]=sad(planes[p],planes[c],hh[c],ww[c],v[2*i],v[2*i+1]);
            wr(out,(size_t)n*8);free(v);free(out);
        }
        else if(op==3){
            int32_t a[7];rd(a,sizeof a);int h=a[0],w=a[1],n=a[5],nc=a[6];
            if(h<1||w<1||n<0||nc<0)return 6;
            uint8_t *p=malloc((size_t)h*w),*c=malloc((size_t)h*w);
            int32_t *tx=malloc((size_t)n*4),*ty=malloc((size_t)n*4);
            int32_t *cand=malloc((size_t)nc*8),*out=calloc((size_t)n*3,4);
            if(!p||!c||!tx||!ty||!cand||!out)return 7;
            rd(p,(size_t)h*w);rd(c,(size_t)h*w);rd(tx,(size_t)n*4);rd(ty,(size_t)n*4);rd(cand,(size_t)nc*8);
            subtiles(p,c,h,w,a[2],a[3],a[4],tx,ty,n,cand,nc,out);
            wr(out,(size_t)n*12);free(p);free(c);free(tx);free(ty);free(cand);free(out);
        }
        else return 8;
    }
    for(int i=0;i<32;i++)free(planes[i]);
    return 0;
}
