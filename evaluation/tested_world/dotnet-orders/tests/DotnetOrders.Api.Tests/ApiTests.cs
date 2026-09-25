using DotnetOrders.Api;
using DotnetOrders.Common;
using Xunit;

namespace DotnetOrders.Api.Tests;

public class ApiTests
{
    [Fact]
    public void ProducesDeterministicDialectV2Messages()
    {
        var messages = Producer.Produce(3).ToList();
        Assert.Equal(3, messages.Count);
        Assert.Equal("order-0001", messages[0].Id);
        Assert.Equal(10m, messages[0].Total);
        Assert.All(messages, message => Assert.Equal(OrderCreated.DialectV2, message.Dialect));
    }

    [Fact]
    public void SpeaksTheFrozenContractDialectAndTopic()
    {
        Assert.Equal(OrderCreated.DialectV2, Contract.Dialect);
        Assert.Equal("orders.events.order-created", Contract.Topic);
    }

    [Fact]
    public void ContractAcceptsPositiveIdBearingMessages()
    {
        Assert.True(Contract.Accepts(new OrderCreated("o-1", 1m, "emea")));
        Assert.False(Contract.Accepts(new OrderCreated("", 1m)));
        Assert.False(Contract.Accepts(new OrderCreated("o-2", 0m)));
    }
}
