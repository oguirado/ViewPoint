export namespace main {
	
	export class RespuestaSrfd {
	    ok: boolean;
	    rc: number;
	    error?: string;
	    datos?: any;
	
	    static createFrom(source: any = {}) {
	        return new RespuestaSrfd(source);
	    }
	
	    constructor(source: any = {}) {
	        if ('string' === typeof source) source = JSON.parse(source);
	        this.ok = source["ok"];
	        this.rc = source["rc"];
	        this.error = source["error"];
	        this.datos = source["datos"];
	    }
	}

}

