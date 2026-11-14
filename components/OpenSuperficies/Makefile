.PHONY: all build test bench clean

all: build test

build:
	CGO_ENABLED=0 go build -trimpath -o bin/ ./cmd/...

test:
	CGO_ENABLED=0 go test -count=1 ./src/...

bench:
	CGO_ENABLED=0 go test -run '^$$' -bench . -benchmem ./src/...

clean:
	rm -rf bin/
