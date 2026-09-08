# Makefile — CapLedger prototype, Package 1 (tier & contract core).
# Targets: all (lib + bench + test binaries), lib, bench, test (builds AND runs
# build/test_core), clean.

CC      := clang
BREW    := $(shell brew --prefix 2>/dev/null || echo /opt/homebrew)
CFLAGS  := -std=c11 -O2 -Wall -Wextra -fPIC -Isrc/include -Isrc/ctier -I$(BREW)/include
LDLIBS  := -lz -llz4 -lzstd -lpthread -L$(BREW)/lib

BUILD   := build
OBJDIR  := $(BUILD)/obj

LIBOBJS := $(OBJDIR)/ctier.o $(OBJDIR)/pool.o $(OBJDIR)/spill.o \
           $(OBJDIR)/hist.o $(OBJDIR)/codec.o $(OBJDIR)/contract.o \
           $(OBJDIR)/norminv.o $(OBJDIR)/feedback_queue.o

.PHONY: all lib bench test clean dirs

all: lib bench $(BUILD)/test_core

lib: $(BUILD)/libctier.dylib

bench: $(BUILD)/calib $(BUILD)/overhead $(BUILD)/feedback $(BUILD)/feedback_async \
	$(BUILD)/tracker_scale

test: $(BUILD)/test_core
	./$(BUILD)/test_core

dirs:
	@mkdir -p $(BUILD) $(BUILD)/tmp $(OBJDIR)

$(OBJDIR)/%.o: src/ctier/%.c src/include/ctier.h src/ctier/internal.h | dirs
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR)/%.o: src/contract/%.c src/include/ctier.h src/include/contract.h \
               src/ctier/internal.h | dirs
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR)/%.o: src/bench/%.c src/include/ctier.h src/include/contract.h | dirs
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR)/%.o: src/tests/%.c src/include/ctier.h src/include/contract.h | dirs
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD)/libctier.dylib: $(LIBOBJS) | dirs
	$(CC) -dynamiclib -o $@ $(LIBOBJS) $(LDLIBS)

$(BUILD)/calib: $(OBJDIR)/calib.o | dirs
	$(CC) -o $@ $(OBJDIR)/calib.o $(LDLIBS)

$(BUILD)/overhead: $(OBJDIR)/overhead.o $(LIBOBJS) | dirs
	$(CC) -o $@ $(OBJDIR)/overhead.o $(LIBOBJS) $(LDLIBS)

$(BUILD)/feedback: $(OBJDIR)/feedback_bench.o $(OBJDIR)/feedback_queue.o \
                   $(OBJDIR)/ctier.o $(OBJDIR)/pool.o \
                   $(OBJDIR)/spill.o $(OBJDIR)/hist.o $(OBJDIR)/codec.o | dirs
	$(CC) -o $@ $(OBJDIR)/feedback_bench.o $(OBJDIR)/feedback_queue.o \
		$(OBJDIR)/ctier.o $(OBJDIR)/pool.o \
		$(OBJDIR)/spill.o $(OBJDIR)/hist.o $(OBJDIR)/codec.o $(LDLIBS)

$(BUILD)/feedback_async: $(OBJDIR)/feedback_async.o $(OBJDIR)/feedback_queue.o | dirs
	$(CC) -o $@ $(OBJDIR)/feedback_async.o $(OBJDIR)/feedback_queue.o -lpthread

$(BUILD)/tracker_scale: $(OBJDIR)/tracker_scale.o $(OBJDIR)/feedback_queue.o | dirs
	$(CC) -o $@ $(OBJDIR)/tracker_scale.o $(OBJDIR)/feedback_queue.o -lpthread

$(BUILD)/test_core: $(OBJDIR)/test_core.o $(LIBOBJS) | dirs
	$(CC) -o $@ $(OBJDIR)/test_core.o $(LIBOBJS) $(LDLIBS)

clean:
	rm -rf $(BUILD)
