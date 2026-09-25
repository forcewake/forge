using DotnetOrders.Common;
using DotnetOrders.Projection;
using Xunit;

namespace DotnetOrders.Projection.Tests;

public class ProjectionTests
{
    private static OrderCreated Message(int n, string? region = "emea") =>
        new($"order-{n:D4}", 10m * n, region);

    [Fact]
    public void AppliesExactlyOneEffectPerMessageId()
    {
        var store = new InMemoryProjectionStore();
        var handler = new ProjectionHandler(store);
        var first = handler.Handle(new Delivery("order-0001", 1, false, Message(1)));
        var duplicate = handler.Handle(new Delivery("order-0001", 2, true, Message(1)));
        Assert.Equal(ProjectionOutcome.Applied, first);
        Assert.Equal(ProjectionOutcome.Duplicate, duplicate);
        Assert.Single(store.Effects());
    }

    [Fact]
    public void CrashAfterCommitBeforeAckRedeliversWithoutASecondEffect()
    {
        // the durable state is committed BEFORE the ack: a consumer
        // that dies between the two restores from the snapshot and
        // sees the broker's redelivery as a Duplicate.
        var store = new InMemoryProjectionStore();
        var handler = new ProjectionHandler(store);
        handler.Handle(new Delivery("order-0002", 7, false, Message(2)));   // commit
        // --- simulated crash: no ack reaches the broker ---------------
        var snapshot = store.Snapshot();
        var restarted = new InMemoryProjectionStore();
        foreach (var effect in snapshot.Split('\n', StringSplitOptions.RemoveEmptyEntries))
        {
            var id = effect.Split(':')[1];
            restarted.TryRecord(id, effect);
        }
        var outcome = new ProjectionHandler(restarted).Handle(
            new Delivery("order-0002", 8, true, Message(2)));              // redelivery
        Assert.Equal(ProjectionOutcome.Duplicate, outcome);
        Assert.Single(restarted.Effects());
    }

    [Fact]
    public void AcceptsBothV1AndV2Shapes()
    {
        var store = new InMemoryProjectionStore();
        var handler = new ProjectionHandler(store);
        var v2 = handler.Handle(new Delivery("order-0003", 1, false, Message(3, "emea")));
        var v1 = handler.Handle(new Delivery("order-0004", 2, false, Message(4, null)));
        Assert.Equal(ProjectionOutcome.Applied, v2);
        Assert.Equal(ProjectionOutcome.Applied, v1);
        Assert.Equal(2, store.Effects().Count);
    }

    [Fact]
    public void RejectsContractViolations()
    {
        var handler = new ProjectionHandler(new InMemoryProjectionStore());
        Assert.Throws<InvalidOperationException>(() =>
            handler.Handle(new Delivery("bad-1", 1, false, new OrderCreated("bad-1", -5m))));
    }
}

public class CompatibilityTests
{
    /// <summary>The NEGATIVE old/new combination: a v1-only consumer
    /// REJECTS the v2 producer for the expected compatibility reason
    /// (the widening field), while the current consumer accepts it.</summary>
    [Fact]
    public void OldV1ConsumerRejectsV2ProducerForTheExpectedReason()
    {
        var v2Message = new OrderCreated("order-0009", 90m, "emea");
        string? reason = null;
        var accepted = V1OnlyGate.Accepts(v2Message, out reason);
        Assert.False(accepted);
        Assert.Equal(V1OnlyGate.RejectionReason, reason);
        // and the current consumer shape accepts the same message
        Assert.True(Contract.Accepts(v2Message));
    }

    [Fact]
    public void OldV1ConsumerStillAcceptsV1Producer()
    {
        string? reason = null;
        Assert.True(V1OnlyGate.Accepts(new OrderCreated("order-0010", 10m), out reason));
        Assert.Null(reason);
    }
}
